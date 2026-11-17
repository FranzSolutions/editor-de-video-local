"""Etapa 2: transcrever a fala com faster-whisper (local, em português, tempo por palavra).

Salva em temp/<video>/:
  transcricao.json  palavras com início/fim (fica guardado para não transcrever de novo)
  legenda.srt       a legenda em blocos de 2 a 4 palavras, para revisar/corrigir
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import sysconfig
import time
from pathlib import Path

from .palavras import FIM_DE_FRASE, Palavra, agrupar, blocos_do_srt, escrever_srt, letras, ler_srt
from .util import ErroEdicao, aviso, duracao, info, num, rodar_ffmpeg

# Frases que o Whisper "inventa" em silêncio ou música no fim de vídeos em português.
ALUCINACOES = ("amara.org", "legendas pela comunidade", "legendado por", "legendas por",
               "transcrição por", "inscreva-se no canal")


def _hash(valor) -> str:
    if isinstance(valor, Path):
        return hashlib.sha1(valor.read_bytes()).hexdigest()
    return hashlib.sha1(json.dumps(valor, sort_keys=True, default=str).encode()).hexdigest()


def chaves(ctx) -> tuple[str, str]:
    """(linha do tempo, transcrição).

    A primeira só muda se o próprio áudio/vídeo mudar: aí uma legenda revisada deixa de
    bater com a fala. A segunda muda também com o modelo e as opções do Whisper: aí só é
    preciso transcrever de novo (a legenda revisada continua valendo).
    """
    def assinatura(p: Path | None):
        if not p:
            return None
        st = p.stat()
        return [str(p.resolve()), st.st_size, st.st_mtime_ns]

    linha = _hash({"video": assinatura(ctx.original), "audio": assinatura(ctx.audio_externo),
                   "trecho": [ctx.inicio, ctx.fim]})
    transcricao = _hash({"linha": linha, "transcricao": ctx.cfg["transcricao"],
                         "audio_cfg": ctx.cfg["audio_externo"] if ctx.audio_externo else None})
    return linha, transcricao


def _ler_json(arq: Path) -> dict | None:
    try:
        return json.loads(arq.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def cache_valido(ctx) -> bool:
    dados = _ler_json(ctx.temp / "transcricao.json")
    return bool(dados) and dados.get("chave") == chaves(ctx)[1]


def executar(video: Path, ctx) -> Path:
    arq_json = ctx.temp / "transcricao.json"
    arq_srt = ctx.temp / "legenda.srt"
    chave_linha, chave = chaves(ctx)

    anterior = _ler_json(arq_json)
    if anterior and anterior.get("chave") == chave:
        info("Usando a transcrição que já estava pronta.")
        dados = anterior
    else:
        wav = ctx.arquivo_temp("audio16k.wav")
        rodar_ffmpeg(ctx, ["-i", video, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav],
                     "extrair o áudio", duracao(video))
        inicio = time.time()
        palavras, dispositivo = _transcrever(wav, ctx)
        info(f"{len(palavras)} palavras em {num(time.time() - inicio)} s ({dispositivo}).")
        dados = {
            "versao": 2,
            "chave": chave,
            "modelo": ctx.cfg["transcricao"]["modelo"],
            "palavras": [vars(p) for p in palavras],
            "srt_gerado": (anterior or {}).get("srt_gerado"),
        }
    ctx.palavras = limpar([Palavra(**p) for p in dados["palavras"]])

    # A legenda.srt é refeita a cada rodada (acompanha o agrupamento do preset) enquanto
    # ninguém mexer nela. Se foi revisada, é mantida; se o vídeo mudou, vira cópia de segurança.
    revisada = arq_srt.exists() and _hash(arq_srt) != dados.get("srt_gerado")
    if revisada and anterior is not None and anterior.get("chave_linha") != chave_linha:
        copia = ctx.temp / f"legenda.revisada-{time.strftime('%Y%m%d-%H%M%S')}.srt"
        arq_srt.replace(copia)
        aviso(f"o vídeo mudou, então a legenda que você corrigiu não bate mais com a fala. "
              f"Ela foi guardada em {copia.name} e uma nova foi gerada.")
        revisada = False
    if revisada:
        info("Usando a legenda revisada (legenda.srt).")
    else:
        escrever_srt(agrupar(ctx.palavras, ctx.cfg["legendas"]), arq_srt)
        dados["srt_gerado"] = _hash(arq_srt)
    dados["chave_linha"] = chave_linha
    arq_json.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")

    carregar_legenda(ctx)
    return arq_json


def limpar(palavras: list[Palavra]) -> list[Palavra]:
    """Conserta dois vícios do Whisper nas palavras com tempo:
    - números e moeda em pedaços: "R" "$" "2" ".400" "20" "%"  ->  "R$" "2.400" "20%"
    - palavra repetida colada na anterior, sem pausa nenhuma: "qual" "Qual"  ->  "qual"
    """
    limpas: list[Palavra] = []
    for p in palavras:
        if limpas:
            ant = limpas[-1]
            a, t = ant.texto, p.texto
            colada = p.inicio - ant.fim < 0.15
            if colada and ((t == "$" and a.endswith("R")) or (t[:1] in "%ºª" and a[-1:].isdigit())
                           or (re.match(r"[.,]\d", t) and a[-1:].isdigit())):
                limpas[-1] = Palavra(ant.inicio, max(ant.fim, p.fim), a + t, min(ant.prob, p.prob))
                continue
            if (p.inicio - ant.fim <= 0.02 and letras(t) and letras(t) == letras(a)
                    and not a.endswith((",", *FIM_DE_FRASE))):
                limpas[-1] = Palavra(ant.inicio, max(ant.fim, p.fim), a, ant.prob)
                continue
        limpas.append(p)
    return limpas


def carregar_legenda(ctx) -> None:
    """Lê a legenda (a revisada, se houver) e monta os blocos com tempo por palavra."""
    fonte = ctx.legenda_srt or (ctx.temp / "legenda.srt")
    trechos = ler_srt(fonte)
    if ctx.legenda_srt and (ctx.inicio or ctx.fim):
        # --legenda vale para o vídeo original inteiro: acerta os tempos para o trecho
        zero = ctx.inicio or 0.0
        limite = ctx.fim - zero if ctx.fim else float("inf")
        trechos = [(max(0.0, a - zero), min(limite, b - zero), t) for a, b, t in trechos
                   if b - zero > 0 and a - zero < limite]
    ctx.blocos = blocos_do_srt(trechos, ctx.palavras)
    if ctx.legenda_srt:
        info(f"Legenda: {ctx.legenda_srt.name} ({len(ctx.blocos)} blocos).")


# ---------------------------------------------------------------- faster-whisper

_cuda_preparado = False


def _preparar_cuda() -> None:
    """No Windows as DLLs do CUDA instaladas pelo pip (nvidia-cublas-cu12) ficam dentro do
    site-packages; o ctranslate2 só as encontra se a pasta estiver no caminho de busca."""
    global _cuda_preparado
    if os.name != "nt" or _cuda_preparado:
        return
    _cuda_preparado = True
    base = Path(sysconfig.get_paths()["purelib"]) / "nvidia"
    for pasta in base.glob("*/bin"):
        os.add_dll_directory(str(pasta))
        os.environ["PATH"] = str(pasta) + os.pathsep + os.environ.get("PATH", "")


def _opcoes_dispositivo(ctx) -> list[tuple[str, str]]:
    opcoes = []
    if not ctx.forcar_cpu:
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() > 0:
                tipos = ctranslate2.get_supported_compute_types("cuda")
                opcoes.append(("cuda", "float16" if "float16" in tipos else "int8_float32"))
        except Exception:
            pass
    opcoes.append(("cpu", "int8"))
    return opcoes


def garantir_modelo(nome: str, raiz: Path) -> str:
    """Devolve a pasta do modelo em modelos/, baixando só se ainda não estiver lá."""
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_VERBOSITY", "error")  # sem aviso de "HF_TOKEN" em inglês
    from faster_whisper.utils import download_model

    pasta = str(raiz / "modelos")
    try:
        return download_model(nome, local_files_only=True, cache_dir=pasta)
    except Exception:
        pass
    info(f"Baixando o modelo de transcrição '{nome}' (só na primeira vez; precisa de internet)...")
    try:
        return download_model(nome, cache_dir=pasta)
    except Exception as e:
        raise ErroEdicao(f"Não consegui baixar o modelo '{nome}'. Confira a internet e tente de novo.\n{e}")


def _transcrever(wav: Path, ctx) -> tuple[list[Palavra], str]:
    _preparar_cuda()
    from faster_whisper import WhisperModel

    cfg = ctx.cfg["transcricao"]
    caminho_modelo = garantir_modelo(cfg["modelo"], ctx.raiz)
    opcoes = _opcoes_dispositivo(ctx)
    for i, (dispositivo, tipo) in enumerate(opcoes):
        try:
            extra = {"cpu_threads": os.cpu_count() or 4} if dispositivo == "cpu" else {}
            modelo = WhisperModel(caminho_modelo, device=dispositivo, compute_type=tipo, **extra)
            palavras = _rodar(modelo, wav, cfg, ctx)
            return palavras, "placa de vídeo" if dispositivo == "cuda" else "processador"
        except ErroEdicao:
            raise
        except Exception as e:
            if i + 1 < len(opcoes):
                aviso(f"não deu para usar a placa de vídeo ({str(e).splitlines()[0][:120]}). "
                      "Transcrevendo pelo processador (mais lento).")
                continue
            raise ErroEdicao(f"Falha ao transcrever: {e}")
    raise AssertionError("inalcançável")


def _rodar(modelo, wav: Path, cfg: dict, ctx) -> list[Palavra]:
    segmentos, dados = modelo.transcribe(
        str(wav),
        language=cfg.get("idioma", "pt"),
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        beam_size=5,
        condition_on_previous_text=False,
        initial_prompt=cfg.get("prompt_inicial") or None,
        hotwords=cfg.get("palavras_chave") or None,
    )
    total = dados.duration or 1
    mostrar = sys.stdout.isatty()
    palavras: list[Palavra] = []
    for seg in segmentos:
        if ctx.progresso_etapa:
            ctx.progresso_etapa(min(1.0, seg.end / total))
        if mostrar:
            print(f"\r      transcrevendo: {min(100, int(seg.end / total * 100))}%   ", end="", flush=True)
        if any(a in seg.text.lower() for a in ALUCINACOES):
            continue
        for w in seg.words or []:
            texto = w.word.strip()
            if texto:
                palavras.append(Palavra(round(w.start, 3), round(w.end, 3), texto,
                                        round(w.probability, 3)))
    if mostrar:
        print("\r" + " " * 40 + "\r", end="", flush=True)

    # garante ordem e duração mínima (o Whisper às vezes devolve palavras encavaladas)
    for anterior, atual in zip(palavras, palavras[1:]):
        if atual.inicio < anterior.fim:
            atual.inicio = anterior.fim
        if atual.fim < atual.inicio + 0.02:
            atual.fim = round(atual.inicio + 0.02, 3)
    return palavras
