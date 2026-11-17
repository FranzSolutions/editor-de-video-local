"""Etapa 3: cortar pausas longas (e, se pedido, vícios de linguagem) usando o tempo das palavras.

Pausas maiores que `pausa_maxima` saem, deixando `margem` antes e depois de cada fala.
Vícios ("hã", "é...", "né"...) saem sempre que a opção estiver ligada, mesmo sem pausa.
Os cortes caem exatamente na divisa dos quadros, então áudio e imagem nunca dessincronizam.
Depois do corte as legendas são reposicionadas na nova linha do tempo.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from .palavras import Bloco, Palavra, letras
from .util import aviso, duracao_video, info, num, rodar_ffmpeg

TAXA_AUDIO = 48000


class MapaCortes:
    """Converte tempos do vídeo antes do corte para o vídeo depois do corte."""

    def __init__(self, trechos: list[tuple[float, float]]):
        self.trechos = trechos
        self.deslocamentos = []
        acumulado = 0.0
        for a, b in trechos:
            self.deslocamentos.append(acumulado)
            acumulado += b - a
        self.duracao = acumulado

    def intervalo(self, ini: float, fim: float) -> tuple[float, float] | None:
        partes = [(max(a, ini), min(b, fim), a, d)
                  for (a, b), d in zip(self.trechos, self.deslocamentos)
                  if max(a, ini) < min(b, fim)]
        if not partes:
            return None
        p0, p1 = partes[0], partes[-1]
        return p0[3] + p0[0] - p0[2], p1[3] + p1[1] - p1[2]


def identificar_vicios(palavras: list[Palavra], lista: list[str]) -> list[Palavra]:
    """Regras da lista (config/presets.yaml):
      "hã"    -> sempre é vício
      "é..."  -> só quando é hesitação: o Whisper escreveu com reticências ou arrastado ("ééé")
      "tipo," -> só quando vem com vírgula/reticências ("tipo, assim")
    Não usamos a duração da palavra: o Whisper às vezes estica um "é" verbo até a pausa
    seguinte, e cortá-lo mudaria o sentido da frase.
    """
    def colapsar(t: str) -> str:  # "ééé" -> "é", "hmmm" -> "hm"
        return re.sub(r"(.)\1+", r"\1", t)

    regras = []
    for item in lista:
        item = str(item).strip().lower()
        if item.endswith(("...", "…")):
            tipo = "hesitacao"
        elif item.endswith(","):
            tipo = "virgula"
        else:
            tipo = "sempre"
        regras.append((colapsar(letras(item)), tipo))

    vicios = []
    for p in palavras:
        bruto = p.texto.strip()
        base = letras(bruto)
        alongada = colapsar(base) != base
        for alvo, tipo in regras:
            if not alvo or colapsar(base) != alvo:
                continue
            if (tipo == "sempre"
                    or (tipo == "hesitacao" and (bruto.endswith(("...", "…")) or alongada))
                    or (tipo == "virgula" and bruto.endswith((",", "...", "…")))):
                vicios.append(p)
                break
    return vicios


def _subtrair(trechos: list[list[float]], remover: list[tuple[float, float]]) -> list[list[float]]:
    resultado = []
    for a, b in trechos:
        pedacos = [[a, b]]
        for ra, rb in remover:
            novos = []
            for pa, pb in pedacos:
                if rb <= pa or ra >= pb:
                    novos.append([pa, pb])
                    continue
                if ra > pa:
                    novos.append([pa, ra])
                if rb < pb:
                    novos.append([rb, pb])
            pedacos = novos
        resultado += pedacos
    return resultado


def calcular_trechos(fala: list[Palavra], vicios: list[Palavra], duracao: float,
                     pausa_maxima: float, margem: float, fps: int,
                     cortar_pausas: bool = True) -> list[tuple[float, float]]:
    remover = sorted((v.inicio, v.fim) for v in vicios)

    if cortar_pausas:
        # 1. junta palavras próximas em "ilhas" de fala (um vício no meio sempre separa)
        ilhas: list[list[float]] = []
        for p in fala:
            if ilhas:
                vao_ini, vao_fim = ilhas[-1][1], p.inicio
                tem_vicio = any(a < vao_fim and b > vao_ini for a, b in remover)
                if vao_fim - vao_ini <= pausa_maxima and not tem_vicio:
                    ilhas[-1][1] = max(ilhas[-1][1], p.fim)
                    continue
            ilhas.append([p.inicio, p.fim])
        # 2. margem de respiro em volta de cada ilha
        trechos = [[max(0.0, a - margem), min(duracao, b + margem)] for a, b in ilhas]
    else:
        trechos = [[0.0, duracao]]  # só vícios: o resto do vídeo fica como está
    # 3. tira os vícios
    trechos = _subtrair(trechos, remover)
    # 4. arredonda para a grade de quadros
    return para_quadros(trechos, duracao, fps)


def para_quadros(trechos, duracao: float, fps: int) -> list[tuple[float, float]]:
    """Arredonda os trechos mantidos para a grade de quadros (para fora, sem comer fala)
    e junta os vizinhos."""
    ultimo_quadro = math.floor(duracao * fps + 1e-6)
    alinhados: list[list[int]] = []
    for a, b in sorted(trechos):
        qa = max(0, math.floor(a * fps + 1e-6))
        qb = min(ultimo_quadro, math.ceil(b * fps - 1e-6))
        if qb - qa < 2:
            continue
        if alinhados and qa <= alinhados[-1][1]:
            alinhados[-1][1] = max(alinhados[-1][1], qb)
        else:
            alinhados.append([qa, qb])
    return [(qa / fps, qb / fps) for qa, qb in alinhados]


def remapear(blocos: list[Bloco], mapa: MapaCortes, vicios: list[Palavra] = ()) -> list[Bloco]:
    def e_vicio(p: Palavra) -> bool:
        return any(min(p.fim, v.fim) - max(p.inicio, v.inicio) > 0.5 * (p.fim - p.inicio)
                   for v in vicios)

    novos = []
    for bloco in blocos:
        palavras = []
        for p in bloco.palavras:
            if e_vicio(p):
                continue
            r = mapa.intervalo(p.inicio, p.fim)
            # some da legenda só a palavra que caiu (quase toda) dentro de um corte
            if r and r[1] - r[0] >= 0.5 * (p.fim - p.inicio):
                palavras.append(Palavra(r[0], r[1], p.texto, p.prob))
        if palavras:
            novos.append(Bloco(palavras))
    return novos


def executar(video: Path, ctx) -> Path:
    cfg = ctx.cfg["cortes"]
    fps = ctx.fps
    dur = duracao_video(video)
    if not ctx.palavras and ctx.trechos_manuais is None:  # sem fala: nada a cortar (fotos etc. ficam)
        aviso("nenhuma fala encontrada; nada para cortar.")
        return video

    cortar_pausas = cfg.get("ativo", True) and ctx.trechos_manuais is None
    if ctx.trechos_manuais is not None:
        # a pessoa escolheu na linha do tempo exatamente o que fica
        vicios = []
        trechos = para_quadros(ctx.trechos_manuais, dur, fps)
        info("Usando os cortes escolhidos na linha do tempo.")
    else:
        vicios = identificar_vicios(ctx.palavras, cfg.get("vicios") or []) if cfg.get("remover_vicios") else []
        if vicios:
            info(f"Vícios removidos ({len(vicios)}): " + ", ".join(v.texto for v in vicios))
        elif not cortar_pausas:
            info("Nenhum vício encontrado.")
            return video
        ids_vicios = {id(v) for v in vicios}
        fala = [p for p in ctx.palavras if id(p) not in ids_vicios]
        trechos = calcular_trechos(fala, vicios, dur, float(cfg["pausa_maxima"]), float(cfg["margem"]),
                                   fps, cortar_pausas)
        if ctx.protegidos:  # fotos e clipes sem fala (da montagem) ficam inteiros
            trechos = para_quadros(list(trechos) + list(ctx.protegidos), dur, fps)
    if not trechos:
        aviso("não sobrou nenhum trecho com fala; mantendo o vídeo inteiro.")
        return video

    mapa = MapaCortes(trechos)
    removido = dur - mapa.duracao
    if removido < 0.05:
        info("Nada para cortar." if ctx.trechos_manuais is not None else "Nenhuma pausa longa para cortar.")
        return video
    if cortar_pausas:
        info(f"{len(trechos)} trechos de fala; {num(removido)} s de pausa removidos "
             f"({num(dur)} s -> {num(mapa.duracao)} s).")
    else:
        info(f"{num(removido)} s removidos ({num(dur)} s -> {num(mapa.duracao)} s).")

    # trim/atrim por quadro e por amostra: cortes exatos, sem deriva de sincronia.
    # Um fade de 8 ms em cada emenda evita estalos no áudio.
    n = len(trechos)
    linhas = [f"[0:v]split={n}" + "".join(f"[vs{i}]" for i in range(n)) + ";",
              f"[0:a]asplit={n}" + "".join(f"[as{i}]" for i in range(n)) + ";"]
    for i, (a, b) in enumerate(trechos):
        qa, qb = round(a * fps), round(b * fps)
        sa, sb = qa * TAXA_AUDIO // fps, qb * TAXA_AUDIO // fps
        fade = f"afade=t=in:d=0.008,afade=t=out:st={(sb - sa) / TAXA_AUDIO - 0.008:.6f}:d=0.008"
        linhas.append(f"[vs{i}]trim=start_frame={qa}:end_frame={qb},setpts=PTS-STARTPTS[v{i}];")
        linhas.append(f"[as{i}]atrim=start_sample={sa}:end_sample={sb},asetpts=PTS-STARTPTS,{fade}[a{i}];")
    linhas.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]")
    script = ctx.trabalho / "filtro_cortes.txt"
    script.write_text("\n".join(linhas), encoding="utf-8")

    (ctx.trabalho / "cortes.json").write_text(json.dumps({
        "trechos_mantidos": trechos,
        "segundos_removidos": round(removido, 3),
        "vicios": [vars(v) for v in vicios],
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    saida = ctx.arquivo_temp("cortado.mkv")
    rodar_ffmpeg(ctx, ["-i", video, "-/filter_complex", script, "-map", "[v]", "-map", "[a]",
                       *ctx.video_intermediario(), *ctx.audio_intermediario(), saida],
                 "cortar", mapa.duracao)
    ctx.blocos = remapear(ctx.blocos, mapa, vicios)
    return saida
