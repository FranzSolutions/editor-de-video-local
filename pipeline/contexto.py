"""Estado de um vídeo sendo editado: preset resolvido, pastas e resultados das etapas."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .palavras import Bloco, Palavra

FORMATOS = {
    "vertical": (1080, 1920),
    "quadrado": (1080, 1080),
    "horizontal": (1920, 1080),
}


@dataclass
class Contexto:
    raiz: Path                 # pasta do projeto (o ffmpeg roda daqui)
    original: Path             # arquivo em entrada/ (nunca é alterado)
    preset: str
    cfg: dict                  # preset já com as opções da linha de comando aplicadas
    temp: Path                 # temp/<video>/: transcrição e legenda (ficam guardadas)
    trabalho: Path             # temp/<video>/run-<preset>/: intermediários (apagados no fim)
    verbose: bool = False
    audio_externo: Path | None = None
    legenda_srt: Path | None = None   # --legenda: .srt escolhido pelo usuário
    inicio: float | None = None       # --inicio / --fim: trecho de um vídeo longo
    fim: float | None = None
    forcar_cpu: bool = False
    pasta_saida: Path | None = None   # onde gravar o resultado (padrão: saida/)
    nome_saida: str | None = None     # nome do arquivo final sem extensão (padrão: <original>_<preset>)
    sobrescrever: bool = True         # o editor automático nunca apaga um vídeo pronto
    progresso_etapa: Callable[[float], None] | None = None  # 0..1 da etapa atual (painel)
    trechos_manuais: list[tuple[float, float]] | None = None  # o que manter, escolhido na linha do tempo
    protegidos: list[tuple[float, float]] = field(default_factory=list)  # fotos/clipes sem fala: nunca cortar

    # Preenchidos pelas etapas
    palavras: list[Palavra] = field(default_factory=list)  # fala, na linha do tempo original
    blocos: list[Bloco] = field(default_factory=list)      # legendas, na linha do tempo atual
    saida_final: Path | None = None
    _contador: int = 0

    @property
    def tamanho(self) -> tuple[int, int]:
        return FORMATOS[self.cfg["formato"]]

    @property
    def fps(self) -> int:
        return int(self.cfg["fps"])

    def arquivo_temp(self, nome: str) -> Path:
        """Arquivo intermediário numerado na ordem das etapas (01_preparado.mkv, ...)."""
        self._contador += 1
        return self.trabalho / f"{self._contador:02d}_{nome}"

    def fonte(self) -> tuple[str, Path | None]:
        """(nome da família para o .ass, arquivo .ttf para o drawtext)."""
        cfg = self.cfg["legendas"]
        arquivo = self.raiz / "assets" / "fontes" / cfg["arquivo_fonte"]
        if arquivo.is_file():
            return cfg["fonte"], arquivo
        reserva = Path("C:/Windows/Fonts/ariblk.ttf")
        return "Arial Black", (reserva if reserva.is_file() else None)

    # Parâmetros de codificação dos arquivos intermediários: qualidade quase sem perda
    # (cada etapa recodifica) e áudio em float para nada estourar antes da normalização.
    def video_intermediario(self) -> list[str]:
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p",
                *CORES_BT709]

    @staticmethod
    def audio_intermediario() -> list[str]:
        return ["-c:a", "pcm_f32le", "-ar", "48000", "-ac", "2"]


CORES_BT709 = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
               "-color_range", "tv"]
