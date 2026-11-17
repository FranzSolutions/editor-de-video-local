"""Painel do Editor: uma página no navegador para mandar vídeos, acompanhar a edição,
assistir o resultado e trocar logo, músicas e tela final.

Roda só neste computador (http://localhost:8765): nenhum vídeo vai para a internet.
Também vigia a pasta entrada/ (como o vigiar.py): o que cair lá aparece na página.

Uso: dois cliques em "Abrir Editor.bat". Para desligar, feche a janela preta.
"""

from __future__ import annotations

import copy
import itertools
import json
import math
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import yaml

import editar
import vigiar
from pipeline import linha_do_tempo, montagem
from pipeline.trilha import EXTENSOES as EXTENSOES_AUDIO
from pipeline.util import ErroEdicao, conferir_ffmpeg, programa, sondar, streams

PORTA = int(os.environ.get("EDITOR_PORTA", "8765"))   # outra porta/pasta só para testes
ENDERECO = f"http://localhost:{PORTA}"
VIGIAR_PASTA = os.environ.get("EDITOR_SEM_VIGIA") != "1"
RAIZ = editar.RAIZ
PAGINA = RAIZ / "painel" / "index.html"
ENVIADOS = RAIZ / "entrada" / "painel"     # cópia dos vídeos mandados pela página
SAIDA = RAIZ / "saida"
ASSETS = RAIZ / "assets"
TRILHAS = ASSETS / "trilhas"
AJUSTES = RAIZ / "config" / "ajustes.yaml"
MARCA_LOGO = ASSETS / ".logo-provisorio"     # existe enquanto o logo for o provisório
MARCA_TRILHAS = TRILHAS / ".provisorias"     # nomes das músicas de teste, uma por linha
BAT = RAIZ / "Abrir Editor.bat"
ICONE = ASSETS / "icone.ico"
PROJETO = RAIZ / "projetos" / os.environ.get("EDITOR_PROJETO", "atual")  # aba Montar
MIDIA = PROJETO / "midia"
PREVIAS = MIDIA / ".previas"
MONTAGENS = RAIZ / "temp" / "montagens"   # cada "Editar este vídeo" da aba Montar, enquanto edita
EXTENSOES_IMAGEM = {".png", ".jpg", ".jpeg", ".webp"}
ATALHO_INICIAR = (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu"
                  / "Programs" / "Startup" / "Editor de Vídeo.lnk")

# nome do preset -> (como aparece na página, grupo, o que ele faz)
TIPOS = {
    "reels": ("Reels / TikTok", "Redes sociais", "Vertical, com legenda, logo, música e tela final."),
    "stories": ("Stories", "Redes sociais", "Vertical, legenda um pouco mais alta, até 60 s."),
    "feed": ("Post do feed", "Redes sociais", "Quadrado, com legenda, logo e tela final."),
    "podcast": ("Corte de podcast", "Redes sociais", "Vertical, sem música; corta só as pausas longas."),
    "youtube": ("Vídeo do YouTube", "YouTube",
                "Deitado e sem pausas, com legenda para o YouTube, Shorts e fotos para a thumbnail."),
    "shorts": ("Short", "YouTube", "Vertical, com legenda, sem logo."),
}
PROPORCAO = {"vertical": "9:16", "quadrado": "1:1", "horizontal": "16:9"}


def rel(caminho: Path) -> str:
    return caminho.relative_to(RAIZ).as_posix()


def descrever(arq: Path) -> dict:
    tipo = {".mp4": "video", ".srt": "legenda", ".jpg": "foto", ".png": "foto", ".txt": "texto"}
    return {"nome": arq.name, "rel": rel(arq), "tipo": tipo.get(arq.suffix.lower(), "arquivo"),
            "url": f"/arquivo?p={urllib.parse.quote(rel(arq))}&v={int(arq.stat().st_mtime)}"}


def _ordem(arq: Path) -> tuple:
    """Vídeo principal primeiro, depois os Shorts (1, 2, 3...), legenda, fotos e textos."""
    short = re.search(r"_short(\d+)", arq.stem)
    grupo = {".mp4": 1 if short else 0, ".srt": 2, ".jpg": 3, ".txt": 4}.get(arq.suffix.lower(), 9)
    return (grupo, int(short.group(1)) if short else 0, arq.name)


def listar_saidas(saida: Path) -> list[Path]:
    """O vídeo pronto; no modo YouTube, tudo da pasta dele (vídeo, legenda, Shorts, fotos)."""
    if saida.parent == SAIDA:
        return [saida]
    return sorted((p for p in saida.parent.iterdir() if p.is_file()), key=_ordem)


def livre(pasta: Path, nome: str) -> Path:
    destino, n = pasta / nome, 2
    while destino.exists():
        destino, n = pasta / f"{Path(nome).stem} ({n}){Path(nome).suffix}", n + 1
    return destino


def nome_seguro(bruto: str | None, extensoes: set[str]) -> str:
    nome = Path(urllib.parse.unquote(bruto or "")).name
    nome = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", nome).strip(" .")
    if not nome or Path(nome).suffix.lower() not in extensoes:
        raise ValueError("Esse tipo de arquivo não serve aqui.")
    return nome


# ---------------------------------------------------------------- aba Montar (projeto)

PROJETO_VAZIO = {"versao": 1, "contador": 0, "midia": [], "clipes": [], "narracoes": [], "musica": None,
                 "tipo": "reels", "revisar": False, "sem_logo": False, "vicios": False}


def _faixa(valor, minimo: float, maximo: float, padrao: float) -> float:
    try:
        v = float(valor)
    except (TypeError, ValueError):
        return padrao
    return padrao if math.isnan(v) else min(maximo, max(minimo, v))


def _ffmpeg(*args) -> None:
    r = subprocess.run([programa("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                        *map(str, args)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise ValueError(f"Não consegui preparar esse arquivo.\n{r.stderr[-400:]}")


class Projeto:
    """Guarda a montagem em projetos/atual/projeto.json. Os arquivos adicionados pela página
    são cópias (os originais no computador não mudam)."""

    def __init__(self):
        self.trava = threading.Lock()

    def ler(self) -> dict:
        try:
            dados = json.loads((PROJETO / "projeto.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            dados = {}
        return {**copy.deepcopy(PROJETO_VAZIO), **dados}

    def gravar(self, dados: dict) -> None:
        PROJETO.mkdir(parents=True, exist_ok=True)
        (PROJETO / "projeto.json").write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")

    def _midia_json(self, m: dict) -> dict:
        j = {k: m.get(k) for k in ("id", "nome", "tipo", "duracao", "largura", "altura", "tem_audio")}
        j["url"] = descrever(PROJETO / m["arquivo"])["url"]
        for chave in ("previa", "miniatura"):
            if m.get(chave) and (PROJETO / m[chave]).exists():
                j[f"{chave}_url"] = descrever(PROJETO / m[chave])["url"]
        return j

    def para_pagina(self, dados: dict | None = None) -> dict:
        dados = dados or self.ler()
        pagina = {k: v for k, v in dados.items() if k not in ("midia", "contador")}
        pagina["midia"] = [self._midia_json(m) for m in dados["midia"] if (PROJETO / m["arquivo"]).exists()]
        return pagina

    def _examinar(self, arq: Path) -> dict:
        ext, rel_arq = arq.suffix.lower(), arq.relative_to(PROJETO).as_posix()
        PREVIAS.mkdir(parents=True, exist_ok=True)
        miniatura = PREVIAS / f"{arq.name}.jpg"  # nome inteiro: "a.mp4" e "a.mov" não se misturam
        base = {"arquivo": rel_arq, "nome": arq.name, "tem_audio": False, "duracao": None}
        if ext in EXTENSOES_IMAGEM:
            from PIL import Image
            try:
                with Image.open(arq) as im:
                    largura, altura = im.size
                    im.thumbnail((360, 360))
                    fundo = Image.new("RGB", im.size, (15, 46, 31))
                    fundo.paste(im.convert("RGBA"), mask=im.convert("RGBA"))
                    fundo.save(miniatura, quality=85)
            except Exception:
                raise ValueError("Não consegui abrir essa imagem.")
            return {**base, "tipo": "imagem", "largura": largura, "altura": altura,
                    "miniatura": miniatura.relative_to(PROJETO).as_posix()}
        info_ = sondar(arq)
        video, audio = streams(info_, "video"), streams(info_, "audio")
        try:
            duracao = float(info_["format"]["duration"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Não consegui ler a duração desse arquivo.")
        if ext in editar.EXTENSOES_VIDEO and video:
            previa = PREVIAS / f"{arq.name}.mp4"
            _ffmpeg("-ss", f"{duracao * 0.1:.2f}", "-i", arq, "-frames:v", "1", "-vf", "scale=-2:180", miniatura)
            # prévia leve que qualquer navegador toca (celular grava em HEVC, 4K...)
            _ffmpeg("-i", arq, "-vf", "scale=-2:480", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                    "-g", "15", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", previa)
            v = video[0]
            return {**base, "tipo": "video", "duracao": round(duracao, 3), "tem_audio": bool(audio),
                    "largura": int(v.get("width", 0)), "altura": int(v.get("height", 0)),
                    "previa": previa.relative_to(PROJETO).as_posix(),
                    "miniatura": miniatura.relative_to(PROJETO).as_posix()}
        if ext in EXTENSOES_AUDIO and audio:
            return {**base, "tipo": "audio", "duracao": round(duracao, 3), "tem_audio": True}
        raise ValueError("Esse arquivo não tem imagem nem som que eu consiga usar.")

    def adicionar(self, recebido: Path, nome: str) -> dict:
        MIDIA.mkdir(parents=True, exist_ok=True)
        destino = livre(MIDIA, nome)
        os.replace(recebido, destino)
        try:
            item = self._examinar(destino)
        except Exception:
            destino.unlink(missing_ok=True)
            raise
        with self.trava:
            dados = self.ler()
            dados["contador"] += 1
            item["id"] = f"m{dados['contador']}"
            dados["midia"].append(item)
            self.gravar(dados)
        return self._midia_json(item)

    def salvar(self, novo: dict) -> dict:
        """Grava a linha do tempo vinda da página, conferindo cada valor."""
        with self.trava:
            dados = self.ler()
            midia = {m["id"]: m for m in dados["midia"]}
            clipes = []
            for c in (novo.get("clipes") or [])[:500]:
                m = midia.get(c.get("midia"))
                if not m or m["tipo"] not in ("video", "imagem"):
                    continue
                if m["tipo"] == "video":
                    ini = _faixa(c.get("ini"), 0, max(0.0, m["duracao"] - 0.1), 0)
                    fim = _faixa(c.get("fim"), ini + 0.1, m["duracao"], m["duracao"])
                else:
                    ini, fim = 0.0, _faixa(c.get("fim"), 0.5, 60, 3)
                clipes.append({"id": str(c.get("id") or f"c{len(clipes)}")[:24], "midia": m["id"],
                               "ini": round(ini, 3), "fim": round(fim, 3),
                               "volume": _faixa(c.get("volume"), 0, 2, 1),
                               "cortar_pausas": bool(c.get("cortar_pausas", m["tipo"] == "video"))})
            narracoes = []
            for n in (novo.get("narracoes") or [])[:200]:
                m = midia.get(n.get("midia"))
                if not m or m["tipo"] != "audio":
                    continue
                ini = _faixa(n.get("ini"), 0, max(0.0, m["duracao"] - 0.1), 0)
                narracoes.append({"id": str(n.get("id") or f"n{len(narracoes)}")[:24], "midia": m["id"],
                                  "pos": round(_faixa(n.get("pos"), 0, 36000, 0), 3), "ini": round(ini, 3),
                                  "fim": round(_faixa(n.get("fim"), ini + 0.1, m["duracao"], m["duracao"]), 3),
                                  "volume": _faixa(n.get("volume"), 0, 2, 1)})
            musica = novo.get("musica") or None
            if musica and midia.get(musica.get("midia"), {}).get("tipo") == "audio":
                musica = {"midia": musica["midia"], "volume": _faixa(musica.get("volume"), 0, 2, 1)}
            else:
                musica = None
            tipo = str(novo.get("tipo") or dados["tipo"])
            dados.update(clipes=clipes, narracoes=narracoes, musica=musica,
                         tipo=tipo if tipo in editar.carregar_presets() and tipo != "padrao" else "reels",
                         revisar=bool(novo.get("revisar")), sem_logo=bool(novo.get("sem_logo")),
                         vicios=bool(novo.get("vicios")))
            self.gravar(dados)
            return self.para_pagina(dados)

    def apagar(self, mid: str) -> dict:
        with self.trava:
            dados = self.ler()
            item = next((m for m in dados["midia"] if m["id"] == mid), None)
            if not item:
                raise ValueError("Esse arquivo não está mais no projeto.")
            for chave in ("arquivo", "previa", "miniatura"):
                if item.get(chave):
                    try:
                        (PROJETO / item[chave]).unlink(missing_ok=True)  # só a cópia do projeto
                    except OSError:
                        pass  # em uso por um vídeo que está editando: sai do projeto do mesmo jeito
            dados["midia"] = [m for m in dados["midia"] if m["id"] != mid]
            dados["clipes"] = [c for c in dados["clipes"] if c["midia"] != mid]
            dados["narracoes"] = [n for n in dados["narracoes"] if n["midia"] != mid]
            if dados["musica"] and dados["musica"]["midia"] == mid:
                dados["musica"] = None
            self.gravar(dados)
            return self.para_pagina(dados)

    def novo(self) -> dict:
        with self.trava:
            if PROJETO.is_dir():  # só cópias feitas pela página e o projeto.json
                shutil.rmtree(PROJETO, ignore_errors=True)  # (um arquivo em uso fica para a próxima)
            self.gravar(copy.deepcopy(PROJETO_VAZIO))
            return self.para_pagina()


# ---------------------------------------------------------------- fila de edição

@dataclass
class Trabalho:
    id: str
    arquivo: Path
    preset: str
    origem: str                         # "pagina" ou "pasta"
    revisar: bool = False
    vicios: bool = False
    sem_logo: bool = False
    estado: str = "fila"                # fila, editando, revisar, pronto, erro, cancelado
    etapa: str = "Na fila"
    progresso: float = 0.0
    criado: float = field(default_factory=time.time)
    inicio: float | None = None
    fim: float | None = None
    saidas: list[Path] = field(default_factory=list)
    erro: str | None = None
    revisao: dict | None = None         # materiais da linha do tempo, enquanto espera revisão
    resposta: dict | None = None        # o que a pessoa ajustou (None = deixar como sugerido)
    revisado: threading.Event = field(default_factory=threading.Event)
    montagem: dict | None = None        # aba Montar: o projeto a juntar antes de editar
    musica: Path | None = None          # música de fundo escolhida na montagem
    volume_musica: float = 1.0
    titulo: str | None = None           # nome que aparece na página

    def json(self, posicao: int | None) -> dict:
        return {
            "id": self.id, "nome": self.titulo or self.arquivo.name, "preset": self.preset,
            "tipo": TIPOS.get(self.preset, (self.preset,))[0], "origem": self.origem,
            "estado": self.estado, "etapa": self.etapa, "progresso": round(self.progresso, 4),
            "posicao": posicao,
            "decorrido": round((self.fim or time.time()) - self.inicio, 1) if self.inicio else None,
            "saidas": [descrever(p) for p in self.saidas if p.exists()],
            "pasta": rel(self.saidas[0].parent) if self.saidas else None,
            "erro": self.erro, "revisao": self.revisao is not None,
        }

    def json_revisao(self) -> dict:
        r = dict(self.revisao)
        r["previa"] = descrever(r["previa"])["url"]
        m = dict(r["miniaturas"])
        m["url"] = descrever(m.pop("arquivo"))["url"]
        r["miniaturas"] = m
        r["nome"] = self.arquivo.name
        return r


class Painel:
    def __init__(self):
        self.trabalhos: dict[str, Trabalho] = {}
        self.fila: queue.Queue[str] = queue.Queue()
        self.ids = itertools.count(1)
        self.ocupados: set[Path] = set()   # arquivos já na fila (a pasta não pega de novo)
        self.trava = threading.Lock()
        self.vigiando = False

    def adicionar(self, arquivo: Path, preset: str, origem: str, revisar=False, vicios=False,
                  sem_logo=False, **extra) -> Trabalho:
        with self.trava:
            t = Trabalho(str(next(self.ids)), arquivo, preset, origem, revisar, vicios, sem_logo, **extra)
            self.trabalhos[t.id] = t
            self.ocupados.add(arquivo)
        self.fila.put(t.id)
        print(f"\n[{vigiar.agora()}] Na fila: {t.titulo or arquivo.name} ({preset})", flush=True)
        return t

    def montar(self, projeto: Projeto) -> Trabalho:
        """Manda a montagem atual para a fila: junta os arquivos e depois edita."""
        dados = projeto.ler()
        if not dados["clipes"]:
            raise ValueError("A linha do tempo está vazia: coloque pelo menos um vídeo ou foto.")
        midia = {m["id"]: m for m in dados["midia"]}
        musica = midia.get((dados["musica"] or {}).get("midia"))
        agora = time.strftime("%H%M%S")
        pasta, k = MONTAGENS / agora, 2
        while pasta.exists():
            pasta, k = MONTAGENS / f"{agora}-{k}", k + 1
        # os arquivos usados ficam presos a este trabalho (ligação no disco, sem ocupar espaço):
        # dá para mexer no projeto, tirar arquivos ou começar outro enquanto este edita
        usados = ({c["midia"] for c in dados["clipes"]} | {n["midia"] for n in dados["narracoes"]}
                  | ({musica["id"]} if musica else set()))
        try:
            for mid in usados:
                origem, copia = PROJETO / midia[mid]["arquivo"], pasta / midia[mid]["arquivo"]
                if not origem.is_file():
                    raise ValueError(f'O arquivo "{midia[mid]["nome"]}" sumiu do projeto: tire e adicione de novo.')
                copia.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(origem, copia)
                except OSError:
                    shutil.copy2(origem, copia)
        except Exception:
            shutil.rmtree(pasta, ignore_errors=True)
            raise
        destino = pasta / time.strftime("montagem %d-%m %Hh%M.mkv")  # sai "montagem 27-09 19h29_reels.mp4"
        return self.adicionar(destino, dados["tipo"], "montagem", revisar=dados["revisar"],
                              vicios=dados["vicios"], sem_logo=dados["sem_logo"], montagem=dados,
                              musica=pasta / musica["arquivo"] if musica else None,
                              volume_musica=float((dados["musica"] or {}).get("volume", 1)),
                              titulo=f"Vídeo montado · {time.strftime('%H:%M')}")

    def estado(self) -> dict:
        with self.trava:
            lista = sorted(self.trabalhos.values(), key=lambda t: t.criado)
        na_fila = [t.id for t in lista if t.estado == "fila"]
        ativos = [t for t in lista if t.estado in ("editando", "revisar")]
        return {
            "vigiando": self.vigiando,
            "atual": ativos[0].json(None) if ativos else None,
            "trabalhos": [t.json(na_fila.index(t.id) + 1 if t.id in na_fila else None)
                          for t in reversed(lista) if t.estado != "cancelado"][:40],
        }


def esperar_revisao(t: Trabalho, ctx, video: Path) -> None:
    """Para a edição depois da transcrição e mostra a linha do tempo até a pessoa decidir."""
    t.etapa = "Preparando a linha do tempo"
    ctx.progresso_etapa = None
    t.revisao = linha_do_tempo.preparar(video, ctx)
    t.resposta = None
    t.revisado.clear()
    t.estado, t.etapa = "revisar", "Esperando você revisar"
    vigiar.tocar_aviso(True)
    t.revisado.wait(timeout=12 * 3600)
    if t.resposta is not None:
        linha_do_tempo.aplicar(ctx, t.resposta["legendas"], t.resposta["cortes"], t.revisao["duracao"])
    t.revisao = None
    t.estado, t.etapa = "editando", "Continuando a edição"


def validar_revisao(dados: dict, duracao: float) -> dict:
    legendas, cortes = [], []
    for l in (dados.get("legendas") or [])[:5000]:
        ini, fim = float(l["ini"]), float(l["fim"])
        if 0 <= ini < fim <= duracao + 1:
            legendas.append({"ini": ini, "fim": fim, "texto": str(l.get("texto", ""))[:200]})
    for c in (dados.get("cortes") or [])[:5000]:
        a, b = float(c[0]), float(c[1])
        if 0 <= a < b <= duracao + 1:
            cortes.append((a, b))
    return {"legendas": legendas, "cortes": cortes}


def terminar(t: Trabalho, saida: Path | None, erro: str | None) -> None:
    t.fim = time.time()
    nome = t.titulo or t.arquivo.name
    if erro is None and saida:
        t.estado, t.progresso, t.etapa = "pronto", 1.0, "Pronto"
        t.saidas = listar_saidas(saida)
        destino = vigiar.EDITADOS
        vigiar.registrar(f"{nome} ({t.preset}) -> {saida.relative_to(SAIDA)}")
    else:
        t.estado, t.etapa = "erro", "Deu erro"
        t.erro = erro or "Não foi possível editar este vídeo."
        destino = vigiar.COM_ERRO
        vigiar.registrar(f"ERRO em {nome}: {t.erro.splitlines()[0]}")
    if t.montagem is not None:
        # a montagem juntada é só um passo intermediário: os arquivos continuam no projeto
        shutil.rmtree(t.arquivo.parent, ignore_errors=True)
    else:
        try:  # o original sai da fila: vai para entrada/editados ou entrada/com-erro
            movido = vigiar.mover(t.arquivo, destino)
            if t.estado == "erro":
                (destino / f"{movido.name}.erro.txt").write_text(
                    f"Não foi possível editar '{t.arquivo.name}' ({t.preset}).\n\n{t.erro}\n", encoding="utf-8")
        except OSError:
            pass
    vigiar.tocar_aviso(t.estado == "pronto")


def editar_trabalho(t: Trabalho) -> None:
    t.estado, t.inicio, t.etapa, t.progresso = "editando", time.time(), "Começando", 0.0
    opcoes = [str(t.arquivo), "--preset", t.preset]
    opcoes += (["--remover-vicios"] if t.vicios else []) + (["--sem-logo"] if t.sem_logo else [])
    if t.musica:
        opcoes += ["--trilha", str(t.musica)]
    args = editar.criar_parser().parse_args(opcoes)
    args.vigiando = True  # nunca apaga um vídeo pronto que já esteja em saida/
    try:
        cfg = editar.montar_cfg(editar.carregar_presets(), args)
    except ErroEdicao as e:
        return terminar(t, None, str(e))
    if t.musica:  # volume escolhido na montagem (100% = o padrão do preset)
        if t.volume_musica <= 0.01:
            cfg["trilha"]["ativo"] = False
        else:
            cfg["trilha"]["volume_db"] = float(cfg["trilha"]["volume_db"]) + 20 * math.log10(t.volume_musica)

    def andamento(fracao: float, texto: str) -> None:
        t.progresso, t.etapa = max(t.progresso, min(1.0, fracao)), texto

    base, peso, protegidos = 0.0, 1.0, None
    if t.montagem is not None:
        t.etapa = "Montando o vídeo"
        t.arquivo.parent.mkdir(parents=True, exist_ok=True)
        ctx_montagem = SimpleNamespace(raiz=RAIZ, verbose=False, trabalho=t.arquivo.parent,
                                       progresso_etapa=lambda f: andamento(0.25 * f, "Montando o vídeo"))
        try:
            _, protegidos = montagem.renderizar(t.montagem, t.arquivo.parent, cfg["formato"], cfg["fps"],
                                                t.arquivo, ctx_montagem)
        except (ErroEdicao, OSError) as e:
            return terminar(t, None, str(e))
        base, peso = 0.25, 0.75

    revisor = (lambda ctx, video: esperar_revisao(t, ctx, video)) if t.revisar else None
    saida, erro = editar.rodar_um(t.arquivo, cfg, args, revisor=revisor, protegidos=protegidos,
                                  ao_progredir=lambda f, texto: andamento(base + peso * f, texto))
    terminar(t, saida, erro)


def trabalhar(painel: Painel) -> None:
    while True:
        t = painel.trabalhos.get(painel.fila.get())
        if t is None or t.estado != "fila":
            continue
        try:
            editar_trabalho(t)
        except Exception as e:  # um vídeo com problema nunca derruba o painel
            t.estado, t.etapa, t.erro, t.fim = "erro", "Deu erro", f"Erro inesperado: {e}", time.time()
        finally:
            painel.ocupados.discard(t.arquivo)


def vigiar_pasta(painel: Painel) -> None:
    trava = vigiar.pegar_trava()  # fica aberta enquanto o painel roda
    if trava is None:
        print("  Atenção: outra janela do editor já está vigiando a pasta entrada.", flush=True)
        return
    painel.vigiando = True
    vigia, pastas, lido = vigiar.Vigia(), {}, 0.0
    while True:
        try:
            if time.time() - lido > 30:  # presets novos criam subpastas novas
                pastas = vigiar.pastas_vigiadas(editar.carregar_presets())
                for pasta in pastas:
                    pasta.mkdir(parents=True, exist_ok=True)
                lido = time.time()
            for pasta, preset in pastas.items():
                for arq, _ in sorted(vigiar.videos_na_pasta(pasta, vigia), key=lambda a: a[1]):
                    if arq not in painel.ocupados and vigia.pronto(arq):
                        vigia.esquecer(arq)
                        painel.adicionar(arq, preset, "pasta")
        except ErroEdicao:
            pass  # presets com erro: tenta de novo depois
        except Exception as e:
            print(f"  Atenção (pasta entrada): {e}", flush=True)
        time.sleep(3)


# ---------------------------------------------------------------- configurações

def criar_atalho(destino: Path, argumentos: str = "", minimizado: bool = False) -> None:
    """Atalho do Windows (.lnk) para o "Abrir Editor.bat", com o ícone do editor."""
    script = ("$ws = New-Object -ComObject WScript.Shell; $l = $ws.CreateShortcut($env:ATALHO); "
              "$l.TargetPath = $env:ALVO; $l.Arguments = $env:ARGUMENTOS; "
              "$l.WorkingDirectory = $env:PASTA; $l.IconLocation = $env:ICONE; "
              "$l.Description = $env:DESCRICAO; if ($env:MINIMIZADO -eq '1') { $l.WindowStyle = 7 }; "
              "$l.Save()")
    ambiente = {**os.environ, "ATALHO": str(destino), "ALVO": str(BAT), "ARGUMENTOS": argumentos,
                "PASTA": str(RAIZ), "ICONE": f"{ICONE},0", "DESCRICAO": "Editor de Vídeo",
                "MINIMIZADO": "1" if minimizado else "0"}
    destino.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       env=ambiente, capture_output=True, text=True)
    if r.returncode != 0 or not destino.exists():
        raise ValueError("Não consegui criar o atalho do Windows.")


def ligar_com_windows(ligar: bool) -> None:
    """Liga (ou não) o editor sozinho quando o computador liga: minimizado, sem abrir o navegador."""
    if ligar:
        criar_atalho(ATALHO_INICIAR, "--sem-navegador", minimizado=True)
    else:
        ATALHO_INICIAR.unlink(missing_ok=True)


def _ajustes() -> dict:
    try:
        return yaml.safe_load(AJUSTES.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _cfg(preset: str) -> dict:
    presets = editar.carregar_presets()
    return editar.mesclar(presets.get("padrao", {}), presets.get(preset, {}))


def _trilhas_provisorias() -> set[str]:
    try:
        return {l.strip() for l in MARCA_TRILHAS.read_text(encoding="utf-8-sig").splitlines() if l.strip()}
    except OSError:
        return set()


def ler_config() -> dict:
    reels, shorts, youtube = _cfg("reels"), _cfg("shorts"), _cfg("youtube")
    logo = ASSETS / reels["logo"]["arquivo"]
    provisorias = _trilhas_provisorias()
    trilhas = sorted(p for p in TRILHAS.glob("*") if p.suffix.lower() in EXTENSOES_AUDIO)

    def linhas(cfg):
        return [{"texto": str(l.get("texto", "")), "cor": str(l.get("cor", "#ffffff")),
                 "tamanho": float(l.get("tamanho", 0.08))}
                for l in cfg["chamada_final"].get("linhas") or []]

    def fundo(cfg):
        c = cfg["chamada_final"]
        return [c.get("cor_fundo") or "#0f2e1f", c.get("cor_fundo_2") or c.get("cor_fundo") or "#0f2e1f"]

    config = {
        "logo": descrever(logo)["url"] if logo.is_file() else None,
        "logo_provisorio": MARCA_LOGO.exists(),
        "logo_canto": bool(reels["logo"].get("ativo", True)),
        "logo_tela_final": bool(reels["chamada_final"].get("logo", True)),
        "com_windows": ATALHO_INICIAR.exists(),
        "trilhas": [{"nome": p.name, "url": descrever(p)["url"], "provisoria": p.name in provisorias}
                    for p in trilhas],
        "trilha_padrao": str(reels["trilha"].get("arquivo") or "aleatoria"),
        "chamada_principal": linhas(reels), "fundo_principal": fundo(reels),
        "chamada_shorts": linhas(shorts), "fundo_shorts": fundo(shorts),
        "shorts_quantidade": int((youtube.get("shorts") or {}).get("quantidade") or 0),
    }
    pendencias = []
    if config["logo_provisorio"] and (config["logo_canto"] or config["logo_tela_final"]):
        pendencias.append("O logo é provisório: troque pelo oficial (ou desligue o logo).")
    if not config["trilhas"]:
        pendencias.append("Ainda não há música de fundo: adicione músicas liberadas para uso.")
    elif not any(not t["provisoria"] for t in config["trilhas"]):
        pendencias.append("A música de fundo é só de teste: adicione músicas liberadas.")
    config["pendencias"] = pendencias
    return config


def _linhas_validas(linhas: list, anteriores: list[dict]) -> list[dict]:
    novas = []
    for i, l in enumerate(linhas[:4]):
        texto = " ".join(str(l.get("texto", "")).split())[:40]
        cor = str(l.get("cor", "#ffffff"))
        if not texto:
            continue
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", cor):
            raise ValueError(f"Cor inválida: {cor}")
        tamanho = (anteriores[min(i, len(anteriores) - 1)].get("tamanho", 0.08) if anteriores else 0.08)
        novas.append({"texto": texto, "cor": cor.lower(), "tamanho": tamanho})
    return novas


def salvar_config(dados: dict) -> None:
    ajustes = _ajustes()
    padrao = ajustes.setdefault("padrao", {})
    if "chamada_principal" in dados:
        anteriores = _cfg("reels")["chamada_final"].get("linhas") or []
        padrao.setdefault("chamada_final", {})["linhas"] = _linhas_validas(dados["chamada_principal"], anteriores)
    if "trilha_padrao" in dados:
        nome = str(dados["trilha_padrao"])
        if nome != "aleatoria" and not (TRILHAS / Path(nome).name).is_file():
            raise ValueError("Essa música não está na pasta de músicas.")
        padrao.setdefault("trilha", {})["arquivo"] = nome if nome == "aleatoria" else Path(nome).name
    if "chamada_shorts" in dados:
        anteriores = _cfg("shorts")["chamada_final"].get("linhas") or []
        ajustes.setdefault("shorts", {}).setdefault("chamada_final", {})["linhas"] = \
            _linhas_validas(dados["chamada_shorts"], anteriores)
    if "shorts_quantidade" in dados:
        n = int(dados["shorts_quantidade"])
        if not 0 <= n <= 5:
            raise ValueError("Escolha de 0 a 5 Shorts por vídeo.")
        ajustes.setdefault("youtube", {}).setdefault("shorts", {})["quantidade"] = n
    if "logo_canto" in dados:
        padrao.setdefault("logo", {})["ativo"] = bool(dados["logo_canto"])
    if "logo_tela_final" in dados:
        padrao.setdefault("chamada_final", {})["logo"] = bool(dados["logo_tela_final"])
    if "com_windows" in dados and bool(dados["com_windows"]) != ATALHO_INICIAR.exists():
        ligar_com_windows(bool(dados["com_windows"]))
    AJUSTES.write_text("# Ajustes feitos pelo painel do editor (valem por cima do presets.yaml).\n"
                       "# Pode apagar este arquivo para voltar ao padrão.\n"
                       + yaml.safe_dump(ajustes, allow_unicode=True, sort_keys=False), encoding="utf-8")


def prontos() -> list[dict]:
    itens = []
    for p in SAIDA.iterdir():
        if p.name.startswith(("_", ".")) or p.name.endswith(".novo.mp4"):
            continue
        if p.is_file() and p.suffix == ".mp4":
            itens.append({"nome": p.stem, "quando": p.stat().st_mtime, "pasta": "saida",
                          "arquivos": [descrever(p)]})
        elif p.is_dir():
            arquivos = sorted((a for a in p.iterdir() if a.is_file()), key=_ordem)
            if arquivos:
                itens.append({"nome": p.name, "quando": max(a.stat().st_mtime for a in arquivos),
                              "pasta": rel(p), "arquivos": [descrever(a) for a in arquivos]})
    return sorted(itens, key=lambda i: -i["quando"])[:60]


# ---------------------------------------------------------------- servidor

class Pedido(BaseHTTPRequestHandler):
    painel: Painel
    projeto: Projeto
    protocol_version = "HTTP/1.1"

    def log_message(self, formato, *args):  # a janela mostra a edição, não cada pedido
        pass

    def _responder(self, codigo: int, corpo: bytes, tipo="application/json; charset=utf-8") -> None:
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(corpo)

    def _json(self, dados, codigo: int = 200) -> None:
        self._responder(codigo, json.dumps(dados, ensure_ascii=False).encode("utf-8"))

    def _erro(self, mensagem: str, codigo: int = 400) -> None:
        self.close_connection = True
        self._json({"erro": mensagem}, codigo)

    def _local(self) -> bool:
        # só aceita pedidos feitos para localhost (protege contra sites maliciosos)
        return (self.headers.get("Host") or "").rsplit(":", 1)[0] in ("localhost", "127.0.0.1")

    def _ler_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            raise ValueError("Pedido grande demais.")
        return json.loads(self.rfile.read(n) or b"{}")

    def _receber(self, destino: Path) -> None:
        """Grava o corpo do pedido direto no disco, aos poucos (vídeos podem ter GB)."""
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            raise ValueError("O arquivo chegou vazio.")
        parcial = destino.with_name(destino.name + ".parcial")
        try:
            with open(parcial, "wb") as f:
                falta = n
                while falta:
                    bloco = self.rfile.read(min(1 << 20, falta))
                    if not bloco:
                        raise ConnectionError("O envio foi interrompido.")
                    f.write(bloco)
                    falta -= len(bloco)
            os.replace(parcial, destino)
        except BaseException:
            parcial.unlink(missing_ok=True)
            raise

    def _servir_arquivo(self, caminho: Path) -> None:
        tamanho = caminho.stat().st_size
        tipo = mimetypes.guess_type(caminho.name)[0] or "application/octet-stream"
        if caminho.suffix.lower() in (".srt", ".txt"):
            tipo = "text/plain; charset=utf-8"
        ini, fim, codigo = 0, tamanho - 1, 200
        faixa = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if faixa and tamanho:
            a, b = faixa.groups()
            if a:
                ini, fim = int(a), min(int(b), tamanho - 1) if b else tamanho - 1
            elif b:
                ini = max(0, tamanho - int(b))
            if ini > fim:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{tamanho}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            codigo = 206
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(fim - ini + 1 if tamanho else 0))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        if codigo == 206:
            self.send_header("Content-Range", f"bytes {ini}-{fim}/{tamanho}")
        self.end_headers()
        if self.command == "HEAD" or not tamanho:
            return
        try:
            with open(caminho, "rb") as f:
                f.seek(ini)
                falta = fim - ini + 1
                while falta > 0:
                    bloco = f.read(min(1 << 20, falta))
                    if not bloco:
                        break
                    self.wfile.write(bloco)
                    falta -= len(bloco)
        except (ConnectionError, OSError):
            pass  # o navegador cancelou (normal ao pular o vídeo)

    def _caminho_permitido(self, bruto: str | None, pastas: tuple[Path, ...]) -> Path | None:
        if not bruto:
            return None
        alvo = (RAIZ / bruto).resolve()
        return alvo if any(alvo.is_relative_to(p) for p in pastas) and alvo.exists() else None

    # ---------------------------------------------------------------- GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._local():
            return self._erro("Acesso só pelo próprio computador.", 403)
        url = urllib.parse.urlsplit(self.path)
        consulta = urllib.parse.parse_qs(url.query)
        rota = url.path
        if rota in ("/", "/index.html"):
            return self._responder(200, PAGINA.read_bytes(), "text/html; charset=utf-8")
        if rota == "/api/ping":
            return self._json({"editor": True})
        if rota == "/api/estado":
            return self._json(self.painel.estado())
        if rota == "/api/tipos":
            presets = editar.carregar_presets()
            tipos = []
            for nome in presets:
                if nome == "padrao":
                    continue
                cfg = editar.mesclar(presets.get("padrao", {}), presets[nome])
                titulo, grupo, descricao = TIPOS.get(nome, (nome, "Outros", ""))
                tipos.append({"nome": nome, "titulo": titulo, "grupo": grupo, "descricao": descricao,
                              "proporcao": PROPORCAO.get(cfg.get("formato"), "9:16")})
            return self._json(tipos)
        if rota == "/api/config":
            return self._json(ler_config())
        if rota == "/api/prontos":
            return self._json(prontos())
        if rota.startswith("/api/revisao/"):
            t = self.painel.trabalhos.get(rota.rsplit("/", 1)[1])
            if not t or t.revisao is None:
                return self._erro("Esse vídeo não está esperando revisão.", 404)
            return self._json(t.json_revisao())
        if rota == "/api/projeto":
            return self._json(self.projeto.para_pagina())
        if rota == "/arquivo":
            alvo = self._caminho_permitido((consulta.get("p") or [None])[0],
                                           (SAIDA, ASSETS, RAIZ / "temp", RAIZ / "projetos"))
            if not alvo or not alvo.is_file():
                return self._erro("Arquivo não encontrado.", 404)
            return self._servir_arquivo(alvo)
        return self._erro("Página não encontrada.", 404)

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        # X-Painel: só a página do editor manda esse cabeçalho (outros sites não conseguem)
        if not self._local() or self.headers.get("X-Painel") != "1":
            return self._erro("Acesso só pelo painel do editor.", 403)
        rota = urllib.parse.urlsplit(self.path).path
        try:
            if rota == "/api/enviar":
                return self._enviar()
            if rota.startswith("/api/revisao/"):
                return self._revisao(rota.rsplit("/", 1)[1])
            if rota.startswith("/api/cancelar/"):
                t = self.painel.trabalhos.get(rota.rsplit("/", 1)[1])
                if not t or t.estado != "fila":
                    return self._erro("Esse vídeo não está mais na fila.")
                t.estado = "cancelado"
                self.painel.ocupados.discard(t.arquivo)
                if t.origem == "pagina":
                    t.arquivo.unlink(missing_ok=True)  # era só a cópia enviada pela página
                if t.montagem is not None:
                    shutil.rmtree(t.arquivo.parent, ignore_errors=True)  # só ligações do projeto
                return self._json({"ok": True})
            if rota == "/api/limpar":
                with self.painel.trava:
                    for tid in [i for i, t in self.painel.trabalhos.items()
                                if t.estado in ("pronto", "erro", "cancelado")]:
                        del self.painel.trabalhos[tid]
                return self._json({"ok": True})
            if rota == "/api/abrir":
                return self._abrir(self._ler_json().get("p"))
            if rota == "/api/config":
                salvar_config(self._ler_json())
                return self._json(ler_config())
            if rota == "/api/logo":
                return self._logo()
            if rota == "/api/trilha":
                return self._trilha()
            if rota == "/api/trilha/apagar":
                return self._apagar_trilha(self._ler_json().get("nome"))
            if rota == "/api/projeto":
                return self._json(self.projeto.salvar(self._ler_json()))
            if rota == "/api/projeto/midia":
                return self._projeto_midia()
            if rota == "/api/projeto/midia/apagar":
                return self._json(self.projeto.apagar(str(self._ler_json().get("id"))))
            if rota == "/api/projeto/novo":
                return self._json(self.projeto.novo())
            if rota == "/api/projeto/exportar":
                self.projeto.salvar(self._ler_json())
                return self._json({"id": self.painel.montar(self.projeto).id})
            return self._erro("Ação desconhecida.", 404)
        except (ValueError, ErroEdicao) as e:
            return self._erro(str(e))
        except (ConnectionError, OSError) as e:
            return self._erro(f"Não deu certo: {e}", 500)

    def _enviar(self):
        preset = self.headers.get("X-Preset") or "reels"
        if preset not in editar.carregar_presets() or preset == "padrao":
            raise ValueError(f"Tipo de vídeo desconhecido: {preset}")
        nome = nome_seguro(self.headers.get("X-Nome"), editar.EXTENSOES_VIDEO)
        ENVIADOS.mkdir(parents=True, exist_ok=True)
        destino = livre(ENVIADOS, nome)
        self._receber(destino)
        t = self.painel.adicionar(destino, preset, "pagina",
                                  revisar=self.headers.get("X-Revisar") == "1",
                                  vicios=self.headers.get("X-Vicios") == "1",
                                  sem_logo=self.headers.get("X-SemLogo") == "1")
        return self._json({"id": t.id})

    def _projeto_midia(self):
        nome = nome_seguro(self.headers.get("X-Nome"),
                           editar.EXTENSOES_VIDEO | EXTENSOES_IMAGEM | EXTENSOES_AUDIO)
        recebendo = PROJETO / "recebendo"
        recebendo.mkdir(parents=True, exist_ok=True)
        temporario = recebendo / f"{time.time_ns()}{Path(nome).suffix}"
        self._receber(temporario)
        try:
            return self._json({"midia": self.projeto.adicionar(temporario, nome)})
        finally:
            temporario.unlink(missing_ok=True)

    def _revisao(self, tid: str):
        t = self.painel.trabalhos.get(tid)
        dados = self._ler_json()
        if not t or t.estado != "revisar" or t.revisao is None:
            raise ValueError("Esse vídeo não está esperando revisão.")
        if not dados.get("pular"):
            t.resposta = validar_revisao(dados, t.revisao["duracao"])
        t.revisado.set()
        return self._json({"ok": True})

    def _abrir(self, bruto):
        alvo = self._caminho_permitido(bruto, (SAIDA, RAIZ / "entrada", ASSETS))
        if not alvo:
            raise ValueError("Pasta não encontrada.")
        if alvo.is_file():
            subprocess.Popen(["explorer", "/select,", str(alvo)])
        else:
            os.startfile(alvo)
        return self._json({"ok": True})

    def _logo(self):
        from PIL import Image
        temporario = ASSETS / "logo-enviado.tmp"
        self._receber(temporario)
        try:
            with Image.open(temporario) as im:
                im.load()
                transparente = im.mode in ("RGBA", "LA") or "transparency" in im.info
                imagem = im.convert("RGBA")
        except Exception:
            raise ValueError("Não consegui abrir essa imagem. Use PNG, JPG ou WebP.")
        finally:
            temporario.unlink(missing_ok=True)
        destino = ASSETS / _cfg("reels")["logo"]["arquivo"]
        if destino.exists():
            os.replace(destino, destino.with_name("logo-anterior.png"))
        imagem.save(destino)
        MARCA_LOGO.unlink(missing_ok=True)
        aviso = None if transparente else "Esse logo não tem fundo transparente: ele vai aparecer com fundo."
        return self._json({"ok": True, "aviso": aviso, "config": ler_config()})

    def _trilha(self):
        nome = nome_seguro(self.headers.get("X-Nome"), EXTENSOES_AUDIO)
        destino = livre(TRILHAS, nome)
        self._receber(destino)
        try:
            if not streams(sondar(destino), "audio"):
                raise ErroEdicao("")
        except ErroEdicao:
            destino.unlink(missing_ok=True)
            raise ValueError("Esse arquivo não tem áudio.")
        return self._json({"ok": True, "config": ler_config()})

    def _apagar_trilha(self, nome):
        alvo = TRILHAS / Path(str(nome or "")).name
        if alvo.suffix.lower() not in EXTENSOES_AUDIO or not alvo.is_file():
            raise ValueError("Música não encontrada.")
        alvo.unlink()
        if ler_config()["trilha_padrao"] == alvo.name:
            salvar_config({"trilha_padrao": "aleatoria"})
        return self._json({"ok": True, "config": ler_config()})


class Servidor(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return  # o navegador desistiu da conexão (trocou de vídeo, fechou a aba): normal
        super().handle_error(request, client_address)


def ja_aberto() -> bool:
    try:
        with urllib.request.urlopen(f"{ENDERECO}/api/ping", timeout=1.5) as r:
            return json.loads(r.read()).get("editor") is True
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    abrir_navegador = "--sem-navegador" not in (argv if argv is not None else sys.argv[1:])
    if ja_aberto():
        if abrir_navegador:
            webbrowser.open(ENDERECO)
        print("O editor já estava aberto: abri a página de novo no navegador.")
        return 0
    try:
        conferir_ffmpeg()
    except ErroEdicao as e:
        print(f"ERRO: {e}")
        return 1
    for pasta in (ENVIADOS, SAIDA, TRILHAS):
        pasta.mkdir(parents=True, exist_ok=True)
    mimetypes.add_type("video/mp4", ".mp4")
    mimetypes.add_type("font/ttf", ".ttf")

    painel = Painel()
    Pedido.painel = painel
    Pedido.projeto = Projeto()
    threading.Thread(target=trabalhar, args=(painel,), daemon=True).start()
    if VIGIAR_PASTA:
        threading.Thread(target=vigiar_pasta, args=(painel,), daemon=True).start()
    try:
        servidor = Servidor(("127.0.0.1", PORTA), Pedido)
    except OSError:
        print(f"ERRO: a porta {PORTA} está sendo usada por outro programa.")
        return 1
    servidor.daemon_threads = True

    print("=" * 64)
    print(" EDITOR DE VÍDEO LIGADO")
    print("=" * 64)
    print(f" A página abriu no navegador: {ENDERECO}")
    print(" (se não abriu, copie esse endereço no navegador)")
    print(" Deixe esta janela aberta. Para desligar o editor, feche-a.")
    print("=" * 64, flush=True)
    if abrir_navegador:
        webbrowser.open(ENDERECO)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nEditor desligado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
