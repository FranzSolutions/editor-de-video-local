"""Separa algumas fotos do vídeo pronto para montar a thumbnail (no Canva, por exemplo).

As fotos saem espalhadas pelo vídeo. Em cada ponto o ffmpeg escolhe, entre ~2 s de
quadros, o mais "típico" (filtro thumbnail): evita pegar olho fechado ou borrão.
"""

from __future__ import annotations

from pathlib import Path

from .util import duracao_video, info, rodar_ffmpeg


def executar(video: Path, ctx) -> Path:
    quantidade = int(ctx.cfg.get("capas") or 0)
    dur = duracao_video(video)
    fotos = []
    for k in range(1, quantidade + 1):
        t = max(0.0, dur * k / (quantidade + 1) - 1)
        foto = video.parent / f"{ctx.original.stem}_capa{k}.jpg"
        rodar_ffmpeg(ctx, ["-ss", f"{t:.2f}", "-i", video, "-t", "2",
                           "-vf", "thumbnail=60,scale='min(1280,iw)':-2", "-frames:v", "1",
                           "-q:v", "2", foto], "separar as fotos")
        fotos.append(foto)
    if fotos:
        info(f"{len(fotos)} fotos para a thumbnail: {fotos[0].name} ...")
    return fotos[0] if fotos else video
