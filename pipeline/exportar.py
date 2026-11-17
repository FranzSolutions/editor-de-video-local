"""Etapa 10: exportar o MP4 final (H.264 + AAC, +faststart) em saida/<original>_<preset>.mp4."""

from __future__ import annotations

import os
from pathlib import Path

from .contexto import CORES_BT709
from .util import ErroEdicao, duracao, marca_trecho, rodar_ffmpeg


def nome_saida(ctx) -> Path:
    nome = ctx.nome_saida or f"{ctx.original.stem}_{ctx.preset}"
    if not ctx.nome_saida and (ctx.inicio is not None or ctx.fim is not None):
        nome += f"_{marca_trecho(ctx.inicio, ctx.fim)}"
    pasta = ctx.pasta_saida or ctx.raiz / "saida"
    caminho, n = pasta / f"{nome}.mp4", 2
    while not ctx.sobrescrever and caminho.exists():
        caminho, n = pasta / f"{nome} ({n}).mp4", n + 1
    return caminho


def executar(video: Path, ctx) -> Path:
    cfg = ctx.cfg["exportar"]
    saida = nome_saida(ctx)
    if saida.resolve() == ctx.original.resolve():
        raise ErroEdicao("A saída cairia em cima do arquivo original; nada foi gravado.")
    saida.parent.mkdir(parents=True, exist_ok=True)

    # grava ao lado e só troca no fim: se algo falhar, o vídeo anterior continua inteiro
    novo = saida.with_name(saida.stem + ".novo.mp4")
    try:
        rodar_ffmpeg(ctx, [
            "-i", video, "-map", "0:v", "-map", "0:a",
            "-c:v", "libx264", "-preset", str(cfg["preset_x264"]), "-crf", str(cfg["crf"]),
            "-profile:v", "high", "-pix_fmt", "yuv420p", *CORES_BT709,
            "-c:a", "aac", "-b:a", f"{int(cfg['audio_kbps'])}k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", novo,
        ], "exportar", duracao(video))
    except BaseException:
        novo.unlink(missing_ok=True)
        raise
    try:
        os.replace(novo, saida)
    except PermissionError:
        raise ErroEdicao(f"Não consegui substituir {saida.name}: ele está aberto em outro programa? "
                         f"O vídeo novo ficou em saida/{novo.name}.")
    ctx.saida_final = saida
    return saida
