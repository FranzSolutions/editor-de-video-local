"""Etapa 6: trilha de fundo em loop, com ducking automático e fade in/out.

O volume da música é calculado em relação à voz (medida em LUFS), então funciona igual
com voz baixa ou alta. Quando alguém fala, o sidechaincompress abaixa a música; quando
para, ela volta. Roda depois da chamada final para a música tocar por baixo dela e
terminar com fade-out junto com o vídeo.
"""

from __future__ import annotations

import math
import random
from pathlib import Path

from .util import ErroEdicao, aviso, duracao, info, medir_loudness, rodar_ffmpeg

EXTENSOES = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus"}
VOZ_SIDECHAIN = -16.0   # a cópia da voz que comanda o ducking é levada a este nível
SEM_VOZ = -45.0         # abaixo disso consideramos que o vídeo não tem fala


def escolher_musica(ctx) -> Path | None:
    nome = str(ctx.cfg["trilha"].get("arquivo") or "aleatoria")
    pasta = ctx.raiz / "assets" / "trilhas"
    opcoes = sorted(p for p in pasta.glob("*") if p.suffix.lower() in EXTENSOES)
    if nome != "aleatoria":
        for candidato in (Path(nome), pasta / nome):
            if candidato.is_file():
                return candidato.resolve()  # o ffmpeg roda a partir da pasta do projeto
        if not opcoes:
            raise ErroEdicao(f"Não encontrei a trilha '{nome}' (procurei também em assets/trilhas/).")
        aviso(f"não encontrei a trilha '{nome}'; sorteando uma de assets/trilhas/.")
    return random.choice(opcoes) if opcoes else None


def executar(video: Path, ctx) -> Path:
    cfg = ctx.cfg["trilha"]
    musica = escolher_musica(ctx)
    if musica is None:
        aviso("nenhuma música em assets/trilhas/; seguindo sem trilha.")
        return video
    info(f"Trilha: {musica.name}")

    dur = duracao(video)
    voz = medir_loudness(ctx, video)["input_i"]
    nivel_musica = medir_loudness(ctx, musica)["input_i"]
    if not math.isfinite(nivel_musica) or nivel_musica < -70:
        aviso(f"'{musica.name}' parece muda; seguindo sem trilha.")
        return video

    tem_voz = math.isfinite(voz) and voz > SEM_VOZ
    alvo = voz + float(cfg["volume_db"]) if tem_voz else -16.0
    ganho_musica = alvo - nivel_musica
    ganho_sidechain = VOZ_SIDECHAIN - voz if tem_voz else 0.0
    fade_in, fade_out = float(cfg["fade_in"]), min(float(cfg["fade_out"]), dur / 2)

    fmt = "aformat=sample_fmts=flt:sample_rates=48000:channel_layouts=stereo"
    filtro = (
        f"[1:a]{fmt},atrim=0:{dur:.3f},asetpts=PTS-STARTPTS,volume={ganho_musica:.2f}dB,"
        f"afade=t=in:st=0:d={fade_in},afade=t=out:st={dur - fade_out:.3f}:d={fade_out}[musica];"
        f"[0:a]{fmt},asplit=2[voz][sc0];"
        f"[sc0]volume={ganho_sidechain:.2f}dB[sc];"
    )
    if cfg.get("ducking", True) and tem_voz:
        filtro += (f"[musica][sc]sidechaincompress=threshold=0.03:ratio={float(cfg['ducking_intensidade'])}:"
                   "attack=20:release=400:makeup=1[musica2];"
                   "[voz][musica2]amix=inputs=2:duration=first:normalize=0[a]")
    else:
        filtro += "[sc]anullsink;[voz][musica]amix=inputs=2:duration=first:normalize=0[a]"

    saida = ctx.arquivo_temp("com_trilha.mkv")
    rodar_ffmpeg(ctx, ["-i", video, "-stream_loop", "-1", "-i", musica, "-filter_complex", filtro,
                       "-map", "0:v", "-map", "[a]", "-c:v", "copy", *ctx.audio_intermediario(),
                       "-t", f"{dur:.3f}", saida],
                 "colocar a trilha", dur)
    return saida
