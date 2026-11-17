"""Funções de apoio usadas por todas as etapas: achar e rodar o ffmpeg, medir arquivos,
mostrar mensagens."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import unicodedata
from functools import lru_cache
from pathlib import Path


class ErroEdicao(Exception):
    """Erro com mensagem pronta para mostrar ao usuário (sem traceback)."""


# ---------------------------------------------------------------- mensagens

def info(msg: str) -> None:
    print(f"      {msg}", flush=True)


def aviso(msg: str) -> None:
    print(f"      Atenção: {msg}", flush=True)


def num(valor: float, casas: int = 1) -> str:
    """Número no formato brasileiro (vírgula decimal)."""
    return f"{valor:.{casas}f}".replace(".", ",")


def slug(texto: str) -> str:
    """Nome seguro para pasta: sem acento, sem espaço, sem símbolo."""
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    t = re.sub(r"[^A-Za-z0-9]+", "-", t).strip("-").lower()
    return t[:60] or "video"


def marca_tempo(segundos: float) -> str:
    """Para nomes de arquivo: 83 -> '01m23s', 82.5 -> '01m22.5s'."""
    m, s = divmod(round(segundos, 1), 60)
    if abs(s - round(s)) < 1e-6:
        return f"{int(m):02d}m{int(round(s)):02d}s"
    return f"{int(m):02d}m{s:04.1f}s"


def marca_trecho(inicio: float | None, fim: float | None) -> str:
    marca = marca_tempo(inicio or 0)
    return marca + (f"-{marca_tempo(fim)}" if fim is not None else "")


def ler_tempo(texto: str) -> float:
    """Aceita '83', '83.5', '1:23', '01:02:03.5' e devolve segundos."""
    texto = texto.strip().replace(",", ".")
    try:
        partes = [float(p) for p in texto.split(":")]
    except ValueError:
        raise ErroEdicao(f"Tempo inválido: '{texto}'. Use segundos (83) ou minutos:segundos (1:23).")
    segundos = 0.0
    for p in partes:
        segundos = segundos * 60 + p
    return segundos


# ---------------------------------------------------------------- ffmpeg

def _procurar_programa(nome: str) -> str | None:
    achado = shutil.which(nome)
    if achado:
        return achado
    # Logo depois de instalar pelo winget o PATH do terminal aberto ainda é o antigo.
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    candidatos = [local / "Microsoft" / "WinGet" / "Links" / f"{nome}.exe"]
    candidatos += sorted(
        (local / "Microsoft" / "WinGet" / "Packages").glob(f"Gyan.FFmpeg*/*/bin/{nome}.exe"),
        reverse=True,
    )
    for c in candidatos:
        if c.is_file():
            return str(c)
    return None


@lru_cache(maxsize=None)
def programa(nome: str) -> str:
    caminho = _procurar_programa(nome)
    if not caminho:
        raise ErroEdicao(
            f"Não encontrei o {nome}. Instale com:\n"
            "        winget install --id Gyan.FFmpeg -e\n"
            "      e abra um terminal novo."
        )
    return caminho


def conferir_ffmpeg() -> None:
    """O editor usa recursos do ffmpeg 7+ (ex.: -/filter_complex)."""
    programa("ffprobe")
    r = subprocess.run([programa("ffmpeg"), "-hide_banner", "-version"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = re.search(r"ffmpeg version n?(\d+)\.", r.stdout)
    if m and int(m.group(1)) < 7:
        raise ErroEdicao(f"Este ffmpeg é antigo (versão {m.group(1)}); o editor precisa da 7 ou mais nova.\n"
                         "        winget upgrade --id Gyan.FFmpeg -e")


def rodar_ffmpeg(ctx, args: list[str], titulo: str, duracao: float | None = None) -> None:
    """Roda o ffmpeg a partir da pasta do projeto (os filtros usam caminhos relativos).

    Com --verbose mostra o comando completo e o log do ffmpeg. Sem ele, mostra só a
    porcentagem e, se der erro, as últimas linhas do log.
    """
    cmd = [programa("ffmpeg"), "-hide_banner", "-nostdin", "-y"]
    cmd += ["-loglevel", "info" if ctx.verbose else "error"]
    if duracao:
        cmd += ["-progress", "pipe:1", "-nostats"]
    cmd += [str(a) for a in args]

    if ctx.verbose:
        print(f"      $ {subprocess.list2cmdline(cmd)}", flush=True)
        for a in args:
            # filtros longos vão para arquivo (-/filter_complex); mostra o conteúdo também
            if isinstance(a, Path) and a.suffix == ".txt" and a.parent == ctx.trabalho:
                print(f"      [{a.name}]\n" + a.read_text(encoding="utf-8"), flush=True)

    proc = subprocess.Popen(
        cmd,
        cwd=ctx.raiz,
        stdout=subprocess.PIPE if duracao else subprocess.DEVNULL,
        stderr=None if ctx.verbose else subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    log: list[str] = []
    leitor = None
    if proc.stderr:
        leitor = threading.Thread(target=lambda: log.extend(proc.stderr), daemon=True)
        leitor.start()

    mostrar = sys.stdout.isatty()
    if duracao and proc.stdout:
        ultimo = -1
        for linha in proc.stdout:
            if not linha.startswith("out_time_us="):
                continue
            valor = linha.split("=", 1)[1].strip()
            if not valor.lstrip("-").isdigit():
                continue
            fracao = max(0.0, min(1.0, int(valor) / 1e6 / duracao))
            if ctx.progresso_etapa:
                ctx.progresso_etapa(fracao)
            pct = int(fracao * 100)
            if mostrar and pct != ultimo:
                print(f"\r      {titulo}: {pct}%   ", end="", flush=True)
                ultimo = pct
        if mostrar:
            print("\r" + " " * (len(titulo) + 20) + "\r", end="", flush=True)

    proc.wait()
    if leitor:
        leitor.join()
    if proc.returncode != 0:
        detalhe = "".join(log[-15:]).strip() or "(sem mensagem; rode com --verbose)"
        raise ErroEdicao(f"O ffmpeg falhou ao {titulo}.\n{detalhe}")


def caminho_filtro(ctx, caminho: Path) -> str:
    """Caminho para usar DENTRO de um filtro do ffmpeg (ass=, fontfile=, textfile=...).

    Os filtros tratam ':' e '\\' como especiais, então usamos o caminho relativo à pasta
    do projeto (de onde o ffmpeg roda), com barras normais e entre aspas simples.
    """
    try:
        rel = os.path.relpath(caminho, ctx.raiz)
    except ValueError:  # outro disco: fica absoluto
        rel = str(caminho)
    rel = rel.replace("\\", "/").replace(":", "\\:")
    if "'" in rel:
        raise ErroEdicao(f"O caminho {caminho} não pode ter apóstrofo (').")
    return f"'{rel}'"


# ---------------------------------------------------------------- sondagem

def sondar(caminho: Path) -> dict:
    cmd = [programa("ffprobe"), "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(caminho)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise ErroEdicao(f"Não consegui ler '{caminho.name}'. É um arquivo de vídeo/áudio válido?\n"
                         f"{r.stderr.strip()}")
    return json.loads(r.stdout or "{}")


def streams(info_: dict, tipo: str) -> list[dict]:
    lista = [s for s in info_.get("streams", []) if s.get("codec_type") == tipo]
    if tipo == "video":  # capa de mp3/m4a vem como "vídeo" de um quadro só
        lista = [s for s in lista if not s.get("disposition", {}).get("attached_pic")]
    return lista


def _segundos(valor) -> float | None:
    try:
        v = float(valor)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _duracao_tag(s: dict) -> float | None:
    # MKV gravado pelo ffmpeg guarda a duração de cada faixa na tag DURATION=00:00:12.345
    tag = (s.get("tags") or {}).get("DURATION")
    if not tag:
        return None
    try:
        h, m, seg = tag.split(":")
        return int(h) * 3600 + int(m) * 60 + float(seg)
    except ValueError:
        return None


def duracao(caminho: Path) -> float:
    info_ = sondar(caminho)
    d = _segundos(info_.get("format", {}).get("duration"))
    if d is None:
        for s in info_.get("streams", []):
            d = _segundos(s.get("duration")) or _duracao_tag(s)
            if d:
                break
    if d is None:
        raise ErroEdicao(f"Não consegui descobrir a duração de '{caminho.name}'.")
    return d


def duracao_video(caminho: Path) -> float:
    """Duração só da faixa de vídeo (a de áudio pode ser uns milissegundos maior)."""
    info_ = sondar(caminho)
    v = streams(info_, "video")
    if v:
        d = _segundos(v[0].get("duration")) or _duracao_tag(v[0])
        if d:
            return d
    return duracao(caminho)


def dimensoes(caminho: Path) -> tuple[int, int]:
    v = streams(sondar(caminho), "video")
    if not v:
        raise ErroEdicao(f"'{caminho.name}' não tem imagem.")
    return int(v[0]["width"]), int(v[0]["height"])


@lru_cache(maxsize=None)
def filtro_disponivel(nome: str) -> bool:
    r = subprocess.run([programa("ffmpeg"), "-hide_banner", "-filters"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return re.search(rf"^\s*\S+\s+{re.escape(nome)}\s", r.stdout, re.M) is not None


# ---------------------------------------------------------------- áudio

def medir_loudness(ctx, caminho: Path, lufs: float = -14, pico: float = -1, lra: float = 11) -> dict:
    """Primeira passada do loudnorm: devolve input_i, input_tp, input_lra, input_thresh,
    target_offset (números). Áudio mudo vem com input_i = -inf."""
    cmd = [programa("ffmpeg"), "-hide_banner", "-nostdin", "-i", str(caminho), "-vn",
           "-af", f"loudnorm=I={lufs}:TP={pico}:LRA={lra}:print_format=json", "-f", "null", "-"]
    if ctx.verbose:
        print(f"      $ {subprocess.list2cmdline(cmd)}", flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=ctx.raiz)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
    if r.returncode != 0 or not m:
        raise ErroEdicao(f"Não consegui medir o volume de '{caminho.name}'.\n{r.stderr[-800:]}")
    bruto = json.loads(m.group(0))
    medidas = {}
    for chave, valor in bruto.items():
        try:
            medidas[chave] = float(valor)
        except (TypeError, ValueError):
            medidas[chave] = valor
    return medidas
