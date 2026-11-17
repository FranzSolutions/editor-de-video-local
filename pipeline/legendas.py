"""Etapa 5: legendas queimadas estilo TikTok.

Gera um .ass a partir dos blocos (2 a 4 palavras por vez), com a palavra que está sendo
falada em destaque (cor_destaque do preset; padrão amarelo #f2b705), fonte grossa, contorno escuro e posição no
terço inferior. Cada bloco entra com um leve "pulo" de escala.
"""

from __future__ import annotations

import re
from pathlib import Path

from .palavras import Bloco
from .util import aviso, caminho_filtro, duracao_video, info, rodar_ffmpeg

TAMANHO_AUTO = {"vertical": 96, "quadrado": 80, "horizontal": 72}
ALTURA_AUTO = {"vertical": 0.70, "quadrado": 0.86, "horizontal": 0.88}


def _cor_ass(hexa: str, alfa: int = 0, estilo: bool = True) -> str:
    """'#f2b705' -> '&H0005B7F2' (campo de estilo) ou '&H05B7F2&' (tag \\c)."""
    h = hexa.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    bgr = f"{b}{g}{r}".upper()
    return f"&H{alfa:02X}{bgr}" if estilo else f"&H{bgr}&"


def _tempo_ass(cs: int) -> str:
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _formatar(texto: str, cfg: dict) -> str:
    t = texto
    if cfg.get("remover_pontuacao", True):
        t = re.sub(r"[.,;:…]+$", "", t)  # mantém ? e !
    if cfg.get("maiusculas", True):
        t = t.upper()
    return t.replace("\\", "").replace("{", "(").replace("}", ")")


def gerar_ass(blocos: list[Bloco], caminho: Path, ctx) -> None:
    cfg = ctx.cfg["legendas"]
    W, H = ctx.tamanho
    formato = ctx.cfg["formato"]
    tamanho = cfg.get("tamanho", "auto")
    tamanho = TAMANHO_AUTO[formato] if tamanho == "auto" else int(tamanho)
    altura = cfg.get("altura", "auto")
    altura = ALTURA_AUTO[formato] if altura == "auto" else float(altura)
    contorno = cfg.get("contorno", "auto")
    contorno = round(tamanho * 0.08) if contorno == "auto" else float(contorno)
    sombra = cfg.get("sombra", "auto")
    sombra = round(tamanho * 0.04) if sombra == "auto" else float(sombra)
    fonte, _ = ctx.fonte()

    cor = _cor_ass(cfg["cor"], estilo=False)
    destaque = _cor_ass(cfg["cor_destaque"], estilo=False)
    estilo = ",".join(str(x) for x in [
        "Legenda", fonte, tamanho,
        _cor_ass(cfg["cor"]), _cor_ass(cfg["cor_destaque"]),
        _cor_ass(cfg["cor_contorno"]), _cor_ass("#000000", alfa=0x70),
        0, 0, 0, 0, 100, 100, 0, 0,          # negrito, itálico, sublinhado, riscado, escala, espaçamento, ângulo
        1, contorno, sombra, 2,              # borda+sombra, contorno, sombra, alinhamento (centro embaixo)
        round(W * 0.06), round(W * 0.06), round(H * (1 - altura)), 1,
    ])

    linhas = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {W}",
        f"PlayResY: {H}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: {estilo}",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    pulo = cfg.get("animacao", True)
    for i, bloco in enumerate(blocos):
        proximo = blocos[i + 1].inicio if i + 1 < len(blocos) else None
        ini = bloco.inicio
        fim = bloco.fim + 0.3
        if proximo is not None:
            if proximo - bloco.fim <= 0.7:  # emenda no próximo bloco: nada de piscar
                fim = proximo
            fim = min(fim, proximo)
            fim = max(fim, min(ini + 0.4, proximo))
        else:
            fim = max(fim, ini + 0.4)

        textos = [_formatar(p.texto, cfg) for p in bloco.palavras]
        for j, p in enumerate(bloco.palavras):
            e_ini = ini if j == 0 else p.inicio
            e_fim = bloco.palavras[j + 1].inicio if j + 1 < len(bloco.palavras) else fim
            e_fim = min(e_fim, fim)  # nunca passa do fim do bloco (nem encosta no próximo)
            cs_ini, cs_fim = round(e_ini * 100), round(e_fim * 100)
            if cs_fim <= cs_ini:
                continue
            partes = [f"{{\\c{destaque}}}{t}{{\\c{cor}}}" if k == j else t
                      for k, t in enumerate(textos) if t]
            texto = " ".join(partes)
            if j == 0 and pulo:
                texto = "{\\fscx85\\fscy85\\t(0,100,\\fscx100\\fscy100)}" + texto
            linhas.append(f"Dialogue: 0,{_tempo_ass(cs_ini)},{_tempo_ass(cs_fim)},Legenda,,0,0,0,,{texto}")

    caminho.write_text("\n".join(linhas) + "\n", encoding="utf-8")


def executar(video: Path, ctx) -> Path:
    if not ctx.blocos:
        aviso("não há fala para legendar; seguindo sem legenda.")
        return video
    arq_ass = ctx.trabalho / "legendas.ass"
    gerar_ass(ctx.blocos, arq_ass, ctx)
    info(f"{len(ctx.blocos)} blocos de legenda.")

    filtro = f"ass=filename={caminho_filtro(ctx, arq_ass)}"
    pasta_fontes = ctx.raiz / "assets" / "fontes"
    if pasta_fontes.is_dir():
        filtro += f":fontsdir={caminho_filtro(ctx, pasta_fontes)}"

    saida = ctx.arquivo_temp("legendado.mkv")
    rodar_ffmpeg(ctx, ["-i", video, "-vf", filtro, "-map", "0:v", "-map", "0:a",
                       *ctx.video_intermediario(), "-c:a", "copy", saida],
                 "queimar as legendas", duracao_video(video))
    return saida
