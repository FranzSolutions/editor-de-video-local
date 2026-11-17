"""Etapa 9: normalizar o volume com loudnorm em duas passadas (padrão das redes: -14 LUFS,
pico máximo -1 dBTP). Primeiro mede, depois aplica a correção exata."""

from __future__ import annotations

import math
from pathlib import Path

from .util import aviso, duracao, info, medir_loudness, num, rodar_ffmpeg


def executar(video: Path, ctx) -> Path:
    cfg = ctx.cfg["normalizar"]
    lufs, pico, lra = float(cfg["lufs"]), float(cfg["pico"]), float(cfg.get("lra", 11))
    m = medir_loudness(ctx, video, lufs, pico, lra)
    if not math.isfinite(m["input_i"]) or m["input_i"] < -70:
        aviso("o áudio está mudo; nada para normalizar.")
        return video
    info(f"Volume medido: {num(m['input_i'])} LUFS -> {num(lufs)} LUFS.")

    # O loudnorm trabalha em blocos de 100 ms: no fim do arquivo ele numera o último bloco
    # parcial como se fosse cheio e empurra os segundos finais ~67 ms para frente. O asetpts
    # renumera pela contagem de amostras e o atrim devolve o tamanho exato que entrou.
    dur = duracao(video)
    filtro = (f"loudnorm=I={lufs}:TP={pico}:LRA={lra}:"
              f"measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
              f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:"
              f"offset={m['target_offset']}:linear=true,aresample=48000,"
              f"asetpts=N/SR/TB,atrim=end_sample={round(dur * 48000)}")
    saida = ctx.arquivo_temp("normalizado.mkv")
    rodar_ffmpeg(ctx, ["-i", video, "-map", "0:v", "-map", "0:a", "-c:v", "copy",
                       "-af", filtro, *ctx.audio_intermediario(), saida],
                 "normalizar o volume", dur)
    return saida
