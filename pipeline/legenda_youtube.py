"""Grava, ao lado do vídeo pronto, um .srt para subir no YouTube (legendas / CC).

Diferente da legenda queimada (2 a 4 palavras), aqui vão frases de até 2 linhas, como o
YouTube mostra. Subir a legenda ajuda quem assiste sem som e a busca do YouTube, e os
tempos já batem com o vídeo cortado.
"""

from __future__ import annotations

from pathlib import Path

from .palavras import agrupar, escrever_srt
from .util import aviso, info

FRASES = {"palavras_min": 6, "palavras_max": 18, "max_caracteres": 84, "pausa_quebra": 1.0}


def executar(video: Path, ctx) -> Path:
    palavras = [p for bloco in ctx.blocos for p in bloco.palavras]
    if not palavras:
        aviso("sem fala; não há legenda para o YouTube.")
        return video
    srt = video.with_suffix(".srt")
    blocos = agrupar(palavras, FRASES)
    escrever_srt(blocos, srt, largura=42)
    info(f"{srt.name} ({len(blocos)} frases). No YouTube Studio: Legendas > Enviar arquivo.")
    return srt
