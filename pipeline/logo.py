"""Etapa 7: colocar o logo (assets/logo.png, com transparência) num canto do vídeo."""

from __future__ import annotations

from pathlib import Path

from .util import aviso, duracao_video, rodar_ffmpeg

POSICOES = {
    "superior_direito": ("W-w-{mx}", "{my}"),
    "superior_esquerdo": ("{mx}", "{my}"),
    "inferior_direito": ("W-w-{mx}", "H-h-{my}"),
    "inferior_esquerdo": ("{mx}", "H-h-{my}"),
}


def arquivo_logo(ctx) -> Path | None:
    arq = Path(ctx.cfg["logo"]["arquivo"])
    if not arq.is_absolute():
        arq = ctx.raiz / "assets" / arq
    return arq if arq.is_file() else None


def executar(video: Path, ctx) -> Path:
    cfg = ctx.cfg["logo"]
    arq = arquivo_logo(ctx)
    if not arq:
        aviso(f"logo não encontrado (assets/{cfg['arquivo']}); seguindo sem logo.")
        return video
    posicao = cfg.get("posicao", "superior_direito")
    if posicao not in POSICOES:
        aviso(f"posição '{posicao}' não existe; usando superior_direito. "
              f"Opções: {', '.join(POSICOES)}.")
        posicao = "superior_direito"

    W, H = ctx.tamanho
    largura = round(W * float(cfg["largura"]) / 2) * 2
    mx, my = round(W * float(cfg["margem_x"])), round(H * float(cfg["margem_y"]))
    x, y = (p.format(mx=mx, my=my) for p in POSICOES[posicao])
    filtro = (f"[1:v]scale={largura}:-2:flags=lanczos,format=rgba,"
              f"colorchannelmixer=aa={float(cfg['opacidade'])}[logo];"
              f"[0:v][logo]overlay={x}:{y}:format=auto,format=yuv420p[v]")

    saida = ctx.arquivo_temp("com_logo.mkv")
    rodar_ffmpeg(ctx, ["-i", video, "-i", arq, "-filter_complex", filtro,
                       "-map", "[v]", "-map", "0:a", *ctx.video_intermediario(), "-c:a", "copy", saida],
                 "colocar o logo", duracao_video(video))
    return saida
