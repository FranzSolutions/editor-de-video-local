"""Etapa 4: deixar no formato do preset (vertical 1080x1920, quadrado 1080x1080 ou
horizontal 1920x1080).

Se o vídeo não encaixa (ex.: gravado deitado indo para Reels), ele fica inteiro no centro
e o espaço que sobra é preenchido com o próprio vídeo ampliado e desfocado — nada de
barras pretas.
"""

from __future__ import annotations

from pathlib import Path

from .util import dimensoes, duracao_video, info, rodar_ffmpeg


def executar(video: Path, ctx) -> Path:
    W, H = ctx.tamanho
    w, h = dimensoes(video)
    if (w, h) == (W, H):
        info(f"Já está em {W}x{H}.")
        return video

    cfg = ctx.cfg.get("fundo", {})
    if abs(w / h - W / H) < 0.01:
        filtro = f"[0:v]scale={W}:{H}:flags=lanczos,setsar=1[v]"
    else:
        # o desfoque é feito numa cópia pequena (4x menor): mesmo resultado, bem mais rápido
        pw, ph = W // 4, H // 4
        escurecer = float(cfg.get("escurecer", 0.08))
        filtro = (
            "[0:v]split=2[fundo][frente];"
            f"[fundo]scale={pw}:{ph}:force_original_aspect_ratio=increase,crop={pw}:{ph},"
            f"gblur=sigma={float(cfg.get('desfoque', 20))},scale={W}:{H}:flags=bicubic,"
            f"eq=brightness={-escurecer}:saturation=1.1[fundo2];"
            f"[frente]scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2:"
            "flags=lanczos[frente2];"
            "[fundo2][frente2]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p[v]"
        )
        info(f"{w}x{h} -> {W}x{H} com fundo desfocado.")

    saida = ctx.arquivo_temp("formato.mkv")
    rodar_ffmpeg(ctx, ["-i", video, "-filter_complex", filtro, "-map", "[v]", "-map", "0:a",
                       *ctx.video_intermediario(), "-c:a", "copy", saida],
                 "ajustar o formato", duracao_video(video))
    return saida
