"""Revisão numa linha do tempo (painel): prepara o que a página mostra — prévia leve do
vídeo, miniaturas, forma de onda do som, cortes sugeridos e legenda — e aplica o que a
pessoa ajustar (quais trechos cortar e o texto de cada legenda).

Tudo na linha do tempo do vídeo antes dos cortes (a mesma da transcrição).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

from .cortar_silencios import calcular_trechos, identificar_vicios, para_quadros
from .palavras import Bloco, Palavra, escrever_srt, ler_srt
from .util import dimensoes, duracao_video, programa, rodar_ffmpeg

PASSO_ONDA = 0.05       # um valor da forma de onda a cada 50 ms
ALTURA_MINIATURA = 72


def _complemento(trechos: list[tuple[float, float]], duracao: float) -> list[tuple[float, float]]:
    """Os buracos entre os trechos (o que sai do vídeo)."""
    buracos, t = [], 0.0
    for a, b in sorted(trechos):
        if a - t > 0.01:
            buracos.append((t, a))
        t = max(t, b)
    if duracao - t > 0.01:
        buracos.append((t, duracao))
    return buracos


def _juntar(intervalos: list[tuple[float, float]]) -> list[tuple[float, float]]:
    juntos: list[list[float]] = []
    for a, b in sorted(intervalos):
        if juntos and a <= juntos[-1][1] + 0.01:
            juntos[-1][1] = max(juntos[-1][1], b)
        else:
            juntos.append([a, b])
    return [(a, b) for a, b in juntos]


def cortes_sugeridos(ctx, duracao: float) -> list[dict]:
    """Os mesmos cortes que o editor faria sozinho, com o motivo de cada um."""
    cfg = ctx.cfg["cortes"]
    vicios = identificar_vicios(ctx.palavras, cfg.get("vicios") or []) if cfg.get("remover_vicios") else []
    if not ctx.palavras or not (cfg.get("ativo", True) or vicios):
        return []
    ids = {id(v) for v in vicios}
    fala = [p for p in ctx.palavras if id(p) not in ids]
    mantidos = calcular_trechos(fala, vicios, duracao, float(cfg["pausa_maxima"]), float(cfg["margem"]),
                                ctx.fps, cfg.get("ativo", True))
    if ctx.protegidos:  # fotos e clipes sem fala da montagem não entram como corte
        mantidos = para_quadros(list(mantidos) + list(ctx.protegidos), duracao, ctx.fps)
    cortes = []
    for a, b in _complemento(mantidos, duracao):
        dentro = [v.texto for v in vicios if v.inicio < b and v.fim > a]
        motivo = f"Vício: {', '.join(dentro)}" if dentro else (
            "Silêncio no começo" if a < 0.01 else "Silêncio no fim" if b > duracao - 0.01 else "Pausa")
        cortes.append({"ini": round(a, 3), "fim": round(b, 3), "motivo": motivo})
    return cortes


def _forma_de_onda(video: Path) -> list[int]:
    taxa = 8000
    r = subprocess.run([programa("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error",
                        "-i", str(video), "-vn", "-ac", "1", "-ar", str(taxa), "-f", "s16le", "-"],
                       capture_output=True)
    amostras = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32)
    bloco = int(taxa * PASSO_ONDA)
    n = len(amostras) // bloco
    if n == 0:
        return []
    picos = np.abs(amostras[: n * bloco].reshape(n, bloco)).max(axis=1)
    referencia = max(float(np.percentile(picos, 99)), 1.0)
    return [int(v) for v in np.clip(picos / referencia * 100, 0, 100)]


def _miniaturas(video: Path, ctx, pasta: Path, duracao: float) -> dict:
    quantidade = max(8, min(90, int(duracao / 2)))
    w, h = dimensoes(video)
    largura = max(2, round(w * ALTURA_MINIATURA / h / 2) * 2)
    rodar_ffmpeg(ctx, ["-i", video, "-vf", f"fps={quantidade / duracao:.6f},scale={largura}:{ALTURA_MINIATURA}",
                       "-q:v", "5", pasta / "m%03d.jpg"], "gerar as miniaturas")
    quadros = sorted(pasta.glob("m*.jpg"))
    sprite = Image.new("RGB", (largura * max(1, len(quadros)), ALTURA_MINIATURA))
    for i, q in enumerate(quadros):
        with Image.open(q) as im:
            sprite.paste(im, (i * largura, 0))
        q.unlink()
    destino = pasta / "miniaturas.jpg"
    sprite.save(destino, quality=80)
    return {"arquivo": destino, "quantidade": len(quadros), "largura": largura, "altura": ALTURA_MINIATURA}


def preparar(video: Path, ctx) -> dict:
    pasta = ctx.trabalho / "revisao"
    pasta.mkdir(parents=True, exist_ok=True)
    duracao = duracao_video(video)
    previa = pasta / "previa.mp4"
    # prévia leve que qualquer navegador toca, com quadro-chave a cada meio segundo (pular é preciso)
    rodar_ffmpeg(ctx, ["-i", video, "-vf", "scale=-2:480", "-c:v", "libx264", "-preset", "ultrafast",
                       "-crf", "30", "-g", "15", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
                       "-movflags", "+faststart", previa], "preparar a prévia", duracao)
    w, h = dimensoes(video)
    return {
        "duracao": round(duracao, 3),
        "previa": previa,
        "proporcao": round(w / h, 4),
        "miniaturas": _miniaturas(video, ctx, pasta, duracao),
        "onda": _forma_de_onda(video),
        "onda_passo": PASSO_ONDA,
        "cortes": cortes_sugeridos(ctx, duracao),
        "legendas": [{"ini": round(a, 3), "fim": round(b, 3), "texto": t}
                     for a, b, t in ler_srt(ctx.legenda_srt or ctx.temp / "legenda.srt")],
    }


def aplicar(ctx, legendas: list[dict], cortes: list[tuple[float, float]], duracao: float) -> None:
    """Grava a legenda revisada e diz ao corte exatamente o que manter."""
    from . import transcrever  # evita import circular

    blocos = [Bloco([Palavra(float(l["ini"]), float(l["fim"]), " ".join(str(l["texto"]).split()))])
              for l in legendas if str(l.get("texto", "")).strip() and float(l["fim"]) > float(l["ini"])]
    escrever_srt(blocos, ctx.temp / "legenda.srt")
    ctx.legenda_srt = None
    transcrever.carregar_legenda(ctx)
    removidos = _juntar([(max(0.0, a), min(duracao, b)) for a, b in cortes if b > a])
    ctx.trechos_manuais = _complemento(removidos, duracao) or [(0.0, duracao)]
