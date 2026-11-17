"""Palavras transcritas, blocos de legenda (2 a 4 palavras) e leitura/escrita de .srt."""

from __future__ import annotations

import bisect
import difflib
import re
from dataclasses import dataclass
from pathlib import Path

from .util import ErroEdicao


@dataclass
class Palavra:
    inicio: float
    fim: float
    texto: str
    prob: float = 1.0


@dataclass
class Bloco:
    """Um pedaço de legenda que aparece de uma vez na tela."""
    palavras: list[Palavra]

    @property
    def inicio(self) -> float:
        return self.palavras[0].inicio

    @property
    def fim(self) -> float:
        return self.palavras[-1].fim

    @property
    def texto(self) -> str:
        return " ".join(p.texto for p in self.palavras)


# Palavras que ficam estranhas sozinhas no fim de um bloco ("vou te mostrar a" / "tabela").
PALAVRAS_FRACAS = {
    "a", "o", "as", "os", "um", "uma", "uns", "umas", "de", "da", "do", "das", "dos",
    "em", "na", "no", "nas", "nos", "e", "ou", "que", "com", "por", "pra", "pro", "para",
    "ao", "à", "às", "aos", "se", "sem", "me", "te", "lhe", "nem", "mas", "é", "eu",
}
FIM_DE_FRASE = (".", "!", "?", "…")


def letras(texto: str) -> str:
    """Só as letras/números, em minúsculas (para comparar palavras)."""
    return re.sub(r"[^\w]", "", texto.lower())


def agrupar(palavras: list[Palavra], cfg: dict) -> list[Bloco]:
    """Divide a fala em blocos de legenda estilo TikTok."""
    min_p = int(cfg.get("palavras_min", 2))
    max_p = int(cfg.get("palavras_max", 4))
    max_c = int(cfg.get("max_caracteres", 18))
    pausa = float(cfg.get("pausa_quebra", 0.35))

    blocos: list[Bloco] = []
    atual: list[Palavra] = []

    def fechar():
        if atual:
            blocos.append(Bloco(list(atual)))
            atual.clear()

    for p in palavras:
        if atual:
            intervalo = p.inicio - atual[-1].fim
            caracteres = len(" ".join(x.texto for x in atual)) + 1 + len(p.texto)
            if intervalo > pausa:
                fechar()
            elif len(atual) >= max_p or caracteres > max_c:
                virgula = max((i for i, x in enumerate(atual[:-1]) if x.texto.endswith(",")),
                              default=None)
                ultima = atual[-1]
                if virgula is not None:
                    # quebra na vírgula: "Primeiro," / "preste atenção" (e não "Primeiro, preste")
                    resto = atual[virgula + 1:]
                    del atual[virgula + 1:]
                    fechar()
                    atual.extend(resto)
                elif (len(atual) >= 2 and letras(ultima.texto) in PALAVRAS_FRACAS
                        and not ultima.texto.endswith((",", *FIM_DE_FRASE))):
                    # não deixa artigo/preposição pendurado ("escolher o" / "produto"):
                    # ele vai para o começo do próximo bloco
                    atual.pop()
                    fechar()
                    atual.append(ultima)
                else:
                    fechar()
        atual.append(p)
        if p.texto.endswith(FIM_DE_FRASE) or (p.texto.endswith(",") and len(atual) >= min_p):
            fechar()
    fechar()
    return blocos


# ---------------------------------------------------------------- .srt

def _tempo_srt(t: float) -> str:
    ms = max(0, round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _em_duas_linhas(texto: str, largura: int) -> list[str]:
    """Quebra no espaço mais perto do meio quando o texto passa da largura."""
    if len(texto) <= largura or " " not in texto:
        return [texto]
    meio = len(texto) / 2
    corte = min((i for i, c in enumerate(texto) if c == " "), key=lambda i: abs(i - meio))
    return [texto[:corte], texto[corte + 1:]]


def escrever_srt(blocos: list[Bloco], caminho: Path, largura: int | None = None) -> None:
    linhas = []
    for i, b in enumerate(blocos, 1):
        texto = _em_duas_linhas(b.texto, largura) if largura else [b.texto]
        linhas += [str(i), f"{_tempo_srt(b.inicio)} --> {_tempo_srt(b.fim)}", *texto, ""]
    caminho.write_bytes("\r\n".join(linhas).encode("utf-8"))


_LINHA_TEMPO = re.compile(r"^\s*(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})\s*-->"
                          r"\s*(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})")


def _decodificar(bruto: bytes) -> str:
    if bruto.startswith((b"\xff\xfe", b"\xfe\xff")):
        return bruto.decode("utf-16")
    try:
        return bruto.decode("utf-8-sig")
    except UnicodeDecodeError:  # salvo como "ANSI" no Bloco de Notas antigo
        return bruto.decode("cp1252")


def ler_srt(caminho: Path) -> list[tuple[float, float, str]]:
    """Lê (início, fim, texto) de cada trecho. Tolera linha em branco faltando entre os
    trechos, UTF-16/ANSI e trechos encavalados (comuns em legenda automática)."""
    linhas = _decodificar(caminho.read_bytes()).replace("\r\n", "\n").replace("\r", "\n").split("\n")

    def seg(h, m, s, ms):
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000

    brutos: list[tuple[float, float, list[str]]] = []
    for i, linha in enumerate(linhas):
        m = _LINHA_TEMPO.match(linha)
        if m:
            # a linha logo antes do tempo é o número deste trecho, não texto do anterior
            if brutos and i > 0 and linhas[i - 1].strip().isdigit() and brutos[-1][2] \
                    and brutos[-1][2][-1] == linhas[i - 1].strip():
                brutos[-1][2].pop()
            g = m.groups()
            brutos.append((seg(*g[:4]), seg(*g[4:]), []))
        elif brutos and linha.strip():
            brutos[-1][2].append(linha.strip())

    trechos = []
    for ini, fim, texto in brutos:
        corpo = re.sub(r"<[^>]+>|\{[^}]*\}", "", " ".join(texto)).strip()
        if corpo and fim > ini:
            trechos.append((ini, fim, corpo))
    if not trechos and any(l.strip() for l in linhas):
        raise ErroEdicao(f"Não consegui ler nenhuma legenda em '{caminho.name}'. As linhas de tempo "
                         "precisam estar no formato 00:00:01,230 --> 00:00:02,000.")
    trechos.sort()
    for i in range(len(trechos) - 1):  # encavalados: cada trecho termina quando o próximo começa
        ini, fim, texto = trechos[i]
        if fim > trechos[i + 1][0]:
            trechos[i] = (ini, max(ini + 0.05, trechos[i + 1][0]), texto)
    return trechos


def _distribuir(tokens: list[str], a: float, b: float) -> list[tuple[float, float]]:
    """Divide [a, b] entre as palavras, proporcional ao tamanho de cada uma."""
    pesos = [len(t) + 2 for t in tokens]
    total, t, tempos = sum(pesos), a, []
    for peso in pesos:
        d = (b - a) * peso / total
        tempos.append((t, t + d))
        t += d
    return tempos


def _alinhar(tokens: list[str], originais: list[Palavra], ini: float, fim: float) -> list[Palavra]:
    """Dá a cada palavra do texto (talvez editado) o tempo da palavra falada correspondente.

    O pareamento é pelo texto (difflib): corrigir a grafia, apagar ou incluir uma palavra
    não bagunça o tempo das outras. Palavra nova ocupa o espaço entre as vizinhas.
    """
    tempos: list[tuple[float, float] | None] = [None] * len(tokens)
    probs = [1.0] * len(tokens)
    if originais:
        a_txt = [letras(t) for t in tokens]
        b_txt = [letras(o.texto) for o in originais]
        sm = difflib.SequenceMatcher(a=a_txt, b=b_txt, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1):
                for k in range(i2 - i1):
                    o = originais[j1 + k]
                    tempos[i1 + k], probs[i1 + k] = (o.inicio, o.fim), o.prob
            elif tag == "replace":
                # quantidades diferentes: casa por semelhança ("tudu" -> "tudo"); o que
                # sobrar é palavra nova e ganha o espaço entre as vizinhas (logo abaixo)
                j = j1
                for i in range(i1, i2):
                    for jj in range(j, j2):
                        if difflib.SequenceMatcher(None, a_txt[i], b_txt[jj]).ratio() >= 0.6:
                            o = originais[jj]
                            tempos[i], probs[i] = (o.inicio, o.fim), o.prob
                            j = jj + 1
                            break

    i = 0
    while i < len(tokens):
        if tempos[i] is not None:
            i += 1
            continue
        j = i
        while j < len(tokens) and tempos[j] is None:
            j += 1
        a = tempos[i - 1][1] if i > 0 else ini
        b = tempos[j][0] if j < len(tokens) else fim
        if b - a < 0.04 * (j - i):  # sem espaço: divide o tempo com a palavra vizinha
            if i > 0:
                i -= 1
                a = tempos[i][0]
            elif j < len(tokens):
                b = tempos[j][1]
                j += 1
        for k, par in enumerate(_distribuir(tokens[i:j], a, max(b, a + 0.04 * (j - i)))):
            tempos[i + k] = par
        i = j

    palavras = []
    for tok, (a, b), prob in zip(tokens, tempos, probs):
        a = min(max(a, ini), fim)
        palavras.append(Palavra(a, min(max(b, a + 0.02), fim), tok, prob))
    return palavras


def blocos_do_srt(trechos: list[tuple[float, float, str]], palavras: list[Palavra]) -> list[Bloco]:
    """Transforma o .srt (talvez editado) em blocos com tempo por palavra."""
    inicios = [t[0] for t in trechos]
    grupos: list[list[Palavra]] = [[] for _ in trechos]
    for p in sorted(palavras, key=lambda p: p.inicio):  # cada palavra fica num trecho só
        meio = (p.inicio + p.fim) / 2
        k = bisect.bisect_right(inicios, meio + 0.001) - 1
        if k >= 0 and meio < trechos[k][1] + 0.001:
            grupos[k].append(p)
    return [Bloco(_alinhar(texto.split(), originais, ini, fim))
            for (ini, fim, texto), originais in zip(trechos, grupos) if texto.split()]
