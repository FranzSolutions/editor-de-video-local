"""Montagem (aba Montar do painel): junta clipes de vídeo e fotos em sequência, com
narrações por cima, num vídeo só — que depois passa pela edição automática de sempre.

Cada clipe é encaixado no formato final (inteiro no centro, com o próprio clipe desfocado
no fundo), com fps e áudio padronizados, para os pedaços emendarem sem solavanco.
Fotos e clipes marcados "sem cortar pausas" voltam como trechos protegidos: o corte de
pausas não mexe neles (senão uma foto, que não tem fala, sumiria inteira).
"""

from __future__ import annotations

from pathlib import Path

from .contexto import FORMATOS
from .util import ErroEdicao, rodar_ffmpeg

FORMATO_AUDIO = "aresample=48000,aformat=sample_fmts=flt:channel_layouts=stereo"


def _encaixe(k: int, largura: int, altura: int, fps: int, duracao: float) -> str:
    pw, ph = largura // 4, altura // 4  # desfoque numa cópia pequena: mesmo efeito, bem mais rápido
    return (f"[{k}:v]split=2[f{k}][b{k}];"
            f"[b{k}]scale={pw}:{ph}:force_original_aspect_ratio=increase,crop={pw}:{ph},gblur=sigma=20,"
            f"scale={largura}:{altura}:flags=bicubic,eq=brightness=-0.08[bb{k}];"
            f"[f{k}]scale={largura}:{altura}:force_original_aspect_ratio=decrease:force_divisible_by=2:"
            f"flags=lanczos[ff{k}];"
            f"[bb{k}][ff{k}]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={fps},format=yuv420p,"
            f"trim=duration={duracao:.3f},setpts=PTS-STARTPTS[v{k}]")


def renderizar(projeto: dict, pasta: Path, formato: str, fps: int, destino: Path,
               ctx) -> tuple[float, list[tuple[float, float]]]:
    """Grava a montagem em `destino`. Devolve (duração, trechos protegidos)."""
    largura, altura = FORMATOS[formato]
    midia = {m["id"]: m for m in projeto["midia"]}
    clipes = [c for c in projeto["clipes"] if c["midia"] in midia]
    if not clipes:
        raise ErroEdicao("A linha do tempo está vazia: coloque pelo menos um vídeo ou foto.")

    entradas: list = []
    filtros: list[str] = []
    pares, protegidos, pos = [], [], 0.0
    for k, c in enumerate(clipes):
        m = midia[c["midia"]]
        arquivo = pasta / m["arquivo"]
        duracao = float(c["fim"]) - float(c["ini"])
        if m["tipo"] == "video":
            entradas += ["-ss", f"{float(c['ini']):.3f}", "-t", f"{duracao:.3f}", "-i", arquivo]
        else:  # foto: vira um vídeo parado com a duração escolhida
            entradas += ["-loop", "1", "-framerate", str(fps), "-t", f"{duracao:.3f}", "-i", arquivo]
        filtros.append(_encaixe(k, largura, altura, fps, duracao))
        if m["tipo"] == "video" and m.get("tem_audio"):
            filtros.append(f"[{k}:a]{FORMATO_AUDIO},volume={float(c.get('volume', 1)):.3f},apad,"
                           f"atrim=0:{duracao:.3f},asetpts=PTS-STARTPTS[a{k}]")
        else:
            filtros.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{duracao:.3f},"
                           f"aformat=sample_fmts=flt:channel_layouts=stereo[a{k}]")
        pares.append(f"[v{k}][a{k}]")
        if m["tipo"] == "imagem" or not c.get("cortar_pausas", True):
            protegidos.append((round(pos, 3), round(pos + duracao, 3)))
        pos += duracao
    filtros.append("".join(pares) + f"concat=n={len(clipes)}:v=1:a=1[vcat][acat]")

    # narrações: cada uma entra na posição escolhida, por cima do som dos clipes
    extras = []
    for j, n in enumerate(x for x in projeto.get("narracoes") or [] if x["midia"] in midia):
        duracao = float(n["fim"]) - float(n["ini"])
        entradas += ["-ss", f"{float(n['ini']):.3f}", "-t", f"{duracao:.3f}", "-i",
                     pasta / midia[n["midia"]]["arquivo"]]
        atraso = int(round(float(n["pos"]) * 1000))
        filtros.append(f"[{len(clipes) + j}:a]{FORMATO_AUDIO},volume={float(n.get('volume', 1)):.3f},"
                       f"adelay={atraso}:all=1[n{j}]")
        extras.append(f"[n{j}]")
    audio = "[acat]"
    if extras:
        filtros.append(f"[acat]{''.join(extras)}amix=inputs={1 + len(extras)}:duration=first:normalize=0[a]")
        audio = "[a]"

    script = ctx.trabalho / "filtro_montagem.txt"
    script.write_text(";\n".join(filtros), encoding="utf-8")
    rodar_ffmpeg(ctx, [*entradas, "-/filter_complex", script, "-map", "[vcat]", "-map", audio,
                       "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",
                       "-c:a", "pcm_f32le", "-ar", "48000", "-ac", "2", "-t", f"{pos:.3f}", destino],
                 "montar o vídeo", pos)
    return pos, protegidos
