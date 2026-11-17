"""Etapa 1: juntar um áudio externo (locução) ao vídeo.

modo "substituir": o som do vídeo sai e entra a locução.
modo "mixar": os dois tocam juntos (volumes configuráveis no preset).
Se a locução for mais longa que o vídeo, a última imagem fica congelada até o áudio acabar.
"""

from __future__ import annotations

import math
from pathlib import Path

from .util import ErroEdicao, aviso, duracao, duracao_video, num, rodar_ffmpeg, sondar, streams

FORMATO_AUDIO = "aresample=48000,aformat=sample_fmts=flt:channel_layouts=stereo"


def executar(video: Path, ctx) -> Path:
    audio = ctx.audio_externo
    if not audio.is_file():
        raise ErroEdicao(f"Não encontrei o áudio '{audio}'.")
    if not streams(sondar(audio), "audio"):
        raise ErroEdicao(f"'{audio.name}' não tem áudio.")

    cfg = ctx.cfg["audio_externo"]
    modo = cfg.get("modo", "substituir")
    dv, da = duracao_video(video), duracao(audio)
    # duração final alinhada à grade de quadros, para imagem e som terminarem juntos
    total = math.ceil(max(dv, da) * ctx.fps - 1e-6) / ctx.fps

    if modo == "substituir":
        filtro = f"[1:a]{FORMATO_AUDIO},volume={cfg['volume_locucao']},apad=whole_dur={total:.3f}[a]"
    elif modo == "mixar":
        filtro = (f"[0:a]volume={cfg['volume_original']}[a0];"
                  f"[1:a]{FORMATO_AUDIO},volume={cfg['volume_locucao']}[a1];"
                  f"[a0][a1]amix=inputs=2:duration=longest:normalize=0,apad=whole_dur={total:.3f}[a]")
    else:
        raise ErroEdicao(f"Modo de áudio '{modo}' não existe. Use 'substituir' ou 'mixar'.")

    if da > dv + 0.05:
        aviso(f"a locução ({num(da)} s) é mais longa que o vídeo ({num(dv)} s): "
              "a última imagem fica parada até o fim do áudio.")
        filtro += f";[0:v]tpad=stop_mode=clone:stop_duration={total - dv:.4f}[v]"
        mapa_v, codec_v = "[v]", ctx.video_intermediario()
    else:
        mapa_v, codec_v = "0:v", ["-c:v", "copy"]

    saida = ctx.arquivo_temp("com_audio.mkv")
    rodar_ffmpeg(ctx, ["-i", video, "-i", audio, "-filter_complex", filtro,
                       "-map", mapa_v, "-map", "[a]", *codec_v, *ctx.audio_intermediario(),
                       "-t", f"{total:.3f}", saida],
                 "juntar o áudio", total)
    return saida
