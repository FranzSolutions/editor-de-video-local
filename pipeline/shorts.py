"""Escolhe sozinho os melhores trechos de um vídeo longo para virar Shorts (30 a 60 s).

Sem internet e sem IA paga: cada trecho que começa e termina em frase completa ganha uma
nota, seguindo o que costuma funcionar em Shorts:
  - números no trecho (R$, %, mil, prazos...): "número real" prende a atenção
  - começa com pergunta ou gancho ("como", "quanto", "você", "nunca", "erro"...)
  - fala com ritmo (poucas pausas longas)
  - não começa no meio de um raciocínio ("e", "mas", "então"...)
  - duração perto de 45 s
Os melhores trechos que não se sobrepõem viram os Shorts.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .palavras import FIM_DE_FRASE, Palavra, letras

GANCHOS = {"como", "quanto", "quantos", "quantas", "qual", "quais", "quando", "onde", "você",
           "voce", "nunca", "ninguém", "ninguem", "cuidado", "atenção", "atencao", "pare", "sabia",
           "imagina", "olha", "descobri", "aprendi", "errei", "erro", "segredo", "verdade", "mentira"}
CONECTIVOS = {"e", "mas", "então", "entao", "porque", "aí", "ai", "daí", "dai", "isso", "também",
              "tambem", "ou", "que", "pois", "depois", "logo", "inclusive", "além", "alem", "tipo"}
NUMEROS_ESCRITOS = {"mil", "milhão", "milhao", "milhões", "milhoes", "bilhão", "bilhões", "reais",
                    "real", "centavos", "porcento", "dobro", "metade", "triplo"}


@dataclass
class Trecho:
    inicio: float
    fim: float
    duracao: float          # duração estimada depois de cortar as pausas
    texto: str
    nota: float
    motivos: list[str] = field(default_factory=list)


def _e_numero(texto: str) -> bool:
    return any(c.isdigit() for c in texto) or "R$" in texto or "%" in texto \
        or letras(texto) in NUMEROS_ESCRITOS


def _frases(palavras: list[Palavra], pausa: float = 1.2) -> list[list[Palavra]]:
    frases, atual = [], []
    for i, p in enumerate(palavras):
        atual.append(p)
        proxima = palavras[i + 1] if i + 1 < len(palavras) else None
        if p.texto.endswith(FIM_DE_FRASE) or proxima is None or proxima.inicio - p.fim > pausa:
            frases.append(atual)
            atual = []
    return frases


def _duracao_cortada(palavras: list[Palavra], pausa_maxima: float, margem: float) -> float:
    """Quanto o trecho vai durar depois que as pausas longas forem cortadas."""
    total = 2 * margem + sum(p.fim - p.inicio for p in palavras)
    for a, b in zip(palavras, palavras[1:]):
        vao = max(0.0, b.inicio - a.fim)
        total += vao if vao <= pausa_maxima else 2 * margem
    return total


def _avaliar(palavras: list[Palavra], primeira: list[Palavra], duracao: float) -> Trecho:
    numeros = sum(_e_numero(p.texto) for p in palavras)
    abertura = " ".join(p.texto for p in primeira)
    p1 = letras(primeira[0].texto)
    p2 = letras(primeira[1].texto) if len(primeira) > 1 else ""
    gancho = abertura.endswith("?") or p1 in GANCHOS or (p1 == "por" and p2 in ("que", "quê"))
    solto = p1 in CONECTIVOS and not gancho
    ritmo = len(palavras) / max(duracao, 1.0)                 # palavras por segundo
    pausas_longas = sum(b.inicio - a.fim > 2.0 for a, b in zip(palavras, palavras[1:]))

    nota = (3.0 * min(1.0, numeros / 3) + 2.0 * gancho
            + 1.5 * min(1.0, max(0.0, (ritmo - 1.8) / 1.4))
            + 0.5 * max(0.0, 1 - abs(duracao - 45) / 30)
            - 1.5 * solto - 0.3 * pausas_longas)
    motivos = []
    if numeros:
        motivos.append(f"tem números ({numeros})")
    if gancho:
        motivos.append("começa com pergunta/gancho")
    if ritmo >= 2.6:
        motivos.append("fala com ritmo")
    if solto:
        motivos.append("começa com conectivo (pode precisar de contexto)")
    return Trecho(palavras[0].inicio, palavras[-1].fim, duracao,
                  " ".join(p.texto for p in palavras), round(nota, 2), motivos)


def escolher(palavras: list[Palavra], quantidade: int, duracao_min: float = 30,
             duracao_max: float = 60, pausa_maxima: float = 0.6, margem: float = 0.15) -> list[Trecho]:
    """Os `quantidade` melhores trechos, sem sobreposição, em ordem de aparição."""
    if quantidade <= 0 or not palavras:
        return []
    frases = _frases(palavras)
    candidatos = []
    for i in range(len(frases)):
        palavras_trecho: list[Palavra] = []
        for j in range(i, len(frases)):
            palavras_trecho = palavras_trecho + frases[j]
            dur = _duracao_cortada(palavras_trecho, pausa_maxima, margem)
            if dur > duracao_max:
                break
            if dur >= duracao_min:
                candidatos.append(_avaliar(palavras_trecho, frases[i], dur))

    escolhidos: list[Trecho] = []
    for c in sorted(candidatos, key=lambda t: -t.nota):
        if all(c.fim <= e.inicio or c.inicio >= e.fim for e in escolhidos):
            escolhidos.append(c)
            if len(escolhidos) == quantidade:
                break
    return sorted(escolhidos, key=lambda t: t.inicio)
