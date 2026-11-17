"""Etapa 8: chamada final — uma tela de 2 a 3 s no fim do vídeo (ex.: "Siga para mais").

A tela é desenhada pelo ffmpeg (fundo em degradê com as cores do preset, logo e textos com
drawtext, entrando com fade). Se `chamada_final.arquivo` apontar para um vídeo pronto
(ex.: uma vinheta feita no Remotion), ele é usado no lugar da tela gerada.
A passagem do vídeo para a chamada é um fade suave.
"""

from __future__ import annotations

from pathlib import Path

from .logo import arquivo_logo
from .util import (ErroEdicao, caminho_filtro, duracao, duracao_video, info, rodar_ffmpeg,
                   sondar, streams)


def _hex(cor: str) -> str:
    return "0x" + cor.lstrip("#")


def _largura_texto(fonte: Path, texto: str, tamanho: int) -> float:
    try:
        from PIL import ImageFont
        return ImageFont.truetype(str(fonte), tamanho).getlength(texto)
    except Exception:
        return len(texto) * tamanho * 0.7


def _gerar_tela(ctx, cfg: dict) -> Path:
    W, H = ctx.tamanho
    fps, dur = ctx.fps, float(cfg["duracao"])
    base = min(W, H)
    _, arq_fonte = ctx.fonte()
    if arq_fonte is None:
        raise ErroEdicao("Nenhuma fonte encontrada para a chamada final (assets/fontes/).")

    linhas = []
    for linha in cfg.get("linhas") or []:
        texto = str(linha.get("texto", "")).strip()
        if not texto:
            continue
        tamanho = round(base * float(linha.get("tamanho", 0.08)))
        largura = _largura_texto(arq_fonte, texto, tamanho)
        if largura > W * 0.86:  # texto comprido: diminui até caber
            tamanho = int(tamanho * W * 0.86 / largura)
        linhas.append((texto, tamanho, linha.get("cor", "#ffffff")))

    logo = arquivo_logo(ctx) if cfg.get("logo", True) else None
    lw = lh = 0
    if logo:
        from PIL import Image
        with Image.open(logo) as im:
            iw, ih = im.size
        lw = round(min(W * 0.55, base * 0.6) / 2) * 2
        lh = lw * ih / iw
        if lh > H * 0.25:
            lw = round(H * 0.25 * iw / ih / 2) * 2
            lh = lw * ih / iw

    # empilha logo, filete dourado e textos, tudo centralizado na vertical
    vao1, vao2 = base * 0.06, base * 0.05
    filete_w, filete_h = round(base * 0.14), max(4, round(base * 0.006))
    alturas_texto = [round(t * 1.25) for _, t, _ in linhas]
    total = (lh + vao1 if logo else 0) + (filete_h + vao2 + sum(alturas_texto) if linhas else 0)
    y = (H - total) / 2

    args: list = []
    fundo2 = cfg.get("cor_fundo_2")
    if fundo2:
        args += ["-f", "lavfi", "-i",
                 f"gradients=s={W}x{H}:r={fps}:d={dur}:type=radial:speed=0:nb_colors=2:"
                 f"c0={_hex(fundo2)}:c1={_hex(cfg['cor_fundo'])}:"
                 f"x0={W // 2}:y0={round(H * 0.45)}:x1=0:y1={H}"]
    else:
        args += ["-f", "lavfi", "-i", f"color=c={_hex(cfg['cor_fundo'])}:s={W}x{H}:r={fps}:d={dur}"]

    partes = ["[0:v]format=rgb24[fundo]"]
    atual = "fundo"
    if logo:
        args += ["-loop", "1", "-framerate", str(fps), "-t", f"{dur}", "-i", logo]
        partes.append(f"[1:v]scale={lw}:-2:flags=lanczos,format=rgba,fade=t=in:st=0.1:d=0.5:alpha=1[lg]")
        partes.append(f"[{atual}][lg]overlay=(W-w)/2:{round(y)}:format=auto[c0]")
        atual = "c0"
        y += lh + vao1

    efeitos = []
    if linhas:
        efeitos.append(f"drawbox=x=(iw-{filete_w})/2:y={round(y)}:w={filete_w}:h={filete_h}:"
                       f"color={_hex('#f2b705')}:t=fill:enable='gte(t,0.35)'")
        y += filete_h + vao2
        fonte_f = caminho_filtro(ctx, arq_fonte)
        for i, ((texto, tamanho, cor), altura) in enumerate(zip(linhas, alturas_texto)):
            arq_txt = ctx.trabalho / f"chamada_{i}.txt"
            arq_txt.write_text(texto, encoding="utf-8")
            t0, d, desl = 0.3 + 0.15 * i, 0.45, round(tamanho * 0.4)
            efeitos.append(
                f"drawtext=fontfile={fonte_f}:textfile={caminho_filtro(ctx, arq_txt)}:expansion=none:"
                f"fontsize={tamanho}:fontcolor={_hex(cor)}:x=(w-text_w)/2:"
                f"y='{round(y)}+{desl}*pow(1-min(1,max(0,t-{t0})/{d}),2)':"
                f"alpha='min(1,max(0,(t-{t0})/{d}))':"
                f"shadowcolor=black@0.35:shadowx=0:shadowy={max(2, round(tamanho * 0.05))}")
            y += altura
    efeitos += ["format=yuv420p", "noise=alls=3:allf=t"]  # ruído leve evita "degraus" no degradê
    partes.append(f"[{atual}]" + ",".join(efeitos) + "[v]")

    script = ctx.trabalho / "filtro_chamada.txt"
    script.write_text(";\n".join(partes), encoding="utf-8")
    tela = ctx.arquivo_temp("tela_chamada.mkv")
    rodar_ffmpeg(ctx, [*args, "-/filter_complex", script, "-map", "[v]",
                       *ctx.video_intermediario(), "-t", f"{dur}", tela],
                 "desenhar a chamada final", dur)
    return tela


def executar(video: Path, ctx) -> Path:
    cfg = ctx.cfg["chamada_final"]
    W, H = ctx.tamanho
    fps = ctx.fps
    transicao = float(cfg.get("transicao", 0.4))

    if cfg.get("arquivo"):
        tela = Path(cfg["arquivo"])
        if not tela.is_absolute():
            tela = ctx.raiz / "assets" / tela
        if not tela.is_file():
            raise ErroEdicao(f"Não encontrei o vídeo da chamada final: {tela}")
        info(f"Usando a chamada pronta {tela.name}.")
    else:
        tela = _gerar_tela(ctx, cfg)

    d_video = duracao_video(video)
    d_tela = duracao(tela)
    tem_audio_tela = bool(streams(sondar(tela), "audio"))

    args: list = ["-i", video, "-i", tela]
    if tem_audio_tela:
        audio_tela = "[1:a]"
    else:
        args += ["-f", "lavfi", "-t", f"{d_tela:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        audio_tela = "[2:a]"
    fmt_a = "aformat=sample_fmts=flt:sample_rates=48000:channel_layouts=stereo"
    filtro = (
        # congela o último quadro durante a transição para não comer o fim da fala
        f"[0:v]tpad=stop_mode=clone:stop_duration={transicao},format=yuv420p,setsar=1,fps={fps}[m];"
        f"[1:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
        f"format=yuv420p,setsar=1,fps={fps}[c];"
        f"[m][c]xfade=transition={cfg.get('efeito', 'fade')}:duration={transicao}:"
        f"offset={d_video:.3f}[v];"
        f"[0:a]{fmt_a}[a0];{audio_tela}{fmt_a},atrim=0:{d_tela:.3f}[a1];"
        "[a0][a1]concat=n=2:v=0:a=1[a]"
    )
    total = d_video + d_tela
    saida = ctx.arquivo_temp("com_chamada.mkv")
    rodar_ffmpeg(ctx, [*args, "-filter_complex", filtro, "-map", "[v]", "-map", "[a]",
                       *ctx.video_intermediario(), *ctx.audio_intermediario(),
                       "-t", f"{total:.3f}", saida],
                 "juntar a chamada final", total)
    return saida
