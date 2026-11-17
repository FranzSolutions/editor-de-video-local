"""Etapa 0 (sempre roda): padroniza o vídeo de entrada para as outras etapas.

- quadros por segundo constantes (celular grava com fps variável, o que atrapalha os cortes)
- rotação do celular aplicada, pixels quadrados, no máximo 1920 px no lado maior
- vídeo HDR do iPhone convertido para cores normais (senão fica desbotado)
- áudio 48 kHz estéreo começando junto com a imagem (ou silêncio, se não houver áudio)
- opcionalmente só um trecho (--inicio / --fim)

O original em entrada/ só é lido, nunca alterado.
"""

from __future__ import annotations

from pathlib import Path

from .util import (ErroEdicao, aviso, filtro_disponivel, rodar_ffmpeg, sondar, streams,
                   _duracao_tag, _segundos)

LADO_MAXIMO = 1920
HDR = {"smpte2084", "arib-std-b67"}


def _rotacao(s: dict) -> int:
    for dado in s.get("side_data_list", []) or []:
        if "rotation" in dado:
            return int(float(dado["rotation"]))
    return int(float((s.get("tags") or {}).get("rotate", 0)))


def executar(video: Path, ctx) -> Path:
    info_ = sondar(video)
    v = streams(info_, "video")
    if not v:
        raise ErroEdicao(f"'{video.name}' não tem imagem (é só áudio?).")
    v = v[0]
    tem_audio = bool(streams(info_, "audio"))

    total = (_segundos(info_.get("format", {}).get("duration"))
             or _segundos(v.get("duration")) or _duracao_tag(v))
    if not total:
        raise ErroEdicao(f"Não consegui descobrir a duração de '{video.name}'.")
    inicio = ctx.inicio or 0.0
    if inicio >= total:
        raise ErroEdicao(f"--inicio ({inicio:.1f}s) passa do fim do vídeo ({total:.1f}s).")
    fim = min(ctx.fim, total) if ctx.fim else total
    if fim <= inicio:
        raise ErroEdicao("--fim precisa ser depois de --inicio.")
    dur = fim - inicio

    # tamanho final já girado e com pixels quadrados
    w, h = int(v["width"]), int(v["height"])
    sar = v.get("sample_aspect_ratio", "1:1")
    try:
        num, den = (int(x) for x in sar.split(":"))
        w = w * num / den if num and den else w
    except ValueError:
        pass
    if abs(_rotacao(v)) in (90, 270):
        w, h = h, w
    escala = min(1.0, LADO_MAXIMO / max(w, h))
    lw, lh = max(2, round(w * escala / 2) * 2), max(2, round(h * escala / 2) * 2)

    filtros_v = []
    if v.get("color_transfer") in HDR:
        if filtro_disponivel("zscale"):
            filtros_v.append("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                             "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv")
        else:
            aviso("vídeo HDR e este ffmpeg não tem zscale: as cores podem ficar desbotadas.")
    filtros_v += [
        f"scale={lw}:{lh}:flags=lanczos:out_color_matrix=bt709:out_range=tv",
        "setsar=1",
        f"fps={ctx.fps}:start_time=0",
        "format=yuv420p",
    ]

    args: list = []
    if inicio:
        args += ["-ss", f"{inicio:.3f}"]
    args += ["-t", f"{dur:.3f}", "-i", video]
    filtro = f"[0:v:0]{','.join(filtros_v)}[v];"
    if tem_audio:
        filtro += "[0:a:0]aresample=48000:async=1:first_pts=0,aformat=sample_fmts=flt:channel_layouts=stereo,apad[a]"
    else:
        aviso("o vídeo não tem áudio; seguindo com silêncio.")
        args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        filtro += "[1:a]aformat=sample_fmts=flt:channel_layouts=stereo[a]"

    saida = ctx.arquivo_temp("preparado.mkv")
    args += ["-filter_complex", filtro, "-map", "[v]", "-map", "[a]",
             *ctx.video_intermediario(), *ctx.audio_intermediario(),
             "-t", f"{dur:.3f}", saida]
    rodar_ffmpeg(ctx, args, "preparar o vídeo", dur)
    return saida
