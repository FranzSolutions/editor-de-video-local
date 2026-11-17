"""Editor automático: fica de olho na pasta entrada/ e edita sozinho todo vídeo que cair lá.

  entrada/              -> Reels (preset reels)
  entrada/youtube/      -> vídeo longo do YouTube: vídeo pronto + legenda .srt + 3 Shorts + fotos
  entrada/<preset>/     -> qualquer outro preset: shorts, stories, feed, podcast...

  O resultado aparece em saida/. O original vai para entrada/editados/ (sem ser alterado);
  se der erro, vai para entrada/com-erro/ junto com um .txt explicando.

O painel (Abrir Editor.bat) já faz isto e mostra tudo numa página; use o vigiar.py só se
quiser vigiar a pasta sem a página: python vigiar.py. Para desligar, feche a janela.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import editar
from pipeline.util import ErroEdicao, conferir_ffmpeg

ENTRADA = editar.RAIZ / "entrada"
EDITADOS = ENTRADA / "editados"
COM_ERRO = ENTRADA / "com-erro"
HISTORICO = editar.RAIZ / "saida" / "_historico.txt"
PRESET_PADRAO = "reels"
INTERVALO = 3            # segundos entre uma olhada e outra na pasta
OLHADAS_ESTAVEL = 2      # tamanho igual em 2 olhadas seguidas = a cópia terminou


def agora() -> str:
    return datetime.now().strftime("%H:%M")


def pastas_vigiadas(presets: dict) -> dict[Path, str]:
    pastas = {ENTRADA: PRESET_PADRAO}
    for nome in presets:
        if nome not in ("padrao", PRESET_PADRAO):
            pastas[ENTRADA / nome] = nome
    return pastas


class Vigia:
    """Só entrega um vídeo quando ele terminou de ser copiado (do celular, WhatsApp, rede...)."""

    def __init__(self):
        self.vistos: dict[Path, tuple[int, int]] = {}  # arquivo -> (tamanho, olhadas iguais)
        self.ignorar: set[Path] = set()                # editados que não deu para mover

    def pronto(self, arq: Path) -> bool:
        if arq in self.ignorar:
            return False
        try:
            tamanho = arq.stat().st_size
        except OSError:
            return False
        anterior, iguais = self.vistos.get(arq, (-1, 0))
        iguais = iguais + 1 if tamanho == anterior else 0
        self.vistos[arq] = (tamanho, iguais)
        if tamanho == 0 or iguais < OLHADAS_ESTAVEL:
            return False
        try:
            with open(arq, "rb+"):  # falha enquanto outro programa ainda grava o arquivo
                pass
        except OSError:
            return iguais >= OLHADAS_ESTAVEL * 5  # arquivo só-leitura: segue depois de ~30 s
        return True

    def esquecer(self, arq: Path) -> None:
        self.vistos.pop(arq, None)


def videos_na_pasta(pasta: Path, vigia: Vigia) -> list[tuple[Path, float]]:
    """(vídeo, hora de modificação) de cada vídeo da pasta; tolera arquivo sumindo no meio."""
    achados = []
    try:
        itens = list(pasta.iterdir())
    except OSError:
        return achados
    for a in itens:
        if (a.suffix.lower() not in editar.EXTENSOES_VIDEO or a.name.startswith((".", "~"))
                or a in vigia.ignorar):
            continue
        try:
            if a.is_file():
                achados.append((a, a.stat().st_mtime))
        except OSError:
            continue
    return achados


def pegar_trava():
    """Só um programa por vez pode vigiar a pasta entrada/ (senão os dois pegariam o mesmo
    vídeo). Devolve o arquivo travado (mantenha-o aberto) ou None se outro já vigia."""
    arq = editar.RAIZ / "temp" / "vigia.lock"
    arq.parent.mkdir(parents=True, exist_ok=True)
    f = open(arq, "a+")
    try:
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except ImportError:
        pass
    except OSError:
        f.close()
        return None
    return f


def mover(arq: Path, pasta: Path) -> Path:
    pasta.mkdir(parents=True, exist_ok=True)
    destino, n = pasta / arq.name, 2
    while destino.exists():
        destino, n = pasta / f"{arq.stem} ({n}){arq.suffix}", n + 1
    shutil.move(str(arq), str(destino))
    return destino


def registrar(texto: str) -> None:
    HISTORICO.parent.mkdir(parents=True, exist_ok=True)
    with HISTORICO.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%d/%m/%Y %H:%M}  {texto}\n")


def tocar_aviso(deu_certo: bool) -> None:
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_OK if deu_certo else winsound.MB_ICONHAND)
    except Exception:
        pass


def editar_um(arq: Path, preset: str, vigia: Vigia) -> None:
    # relê os presets a cada vídeo: mudanças no presets.yaml valem na hora
    args = editar.criar_parser().parse_args([str(arq), "--preset", preset])
    args.vigiando = True  # nunca apaga um vídeo pronto que já esteja em saida/
    cfg = editar.montar_cfg(editar.carregar_presets(), args)
    print(f"\n[{agora()}] Chegou: {arq.relative_to(ENTRADA)}", flush=True)

    saida, erro = editar.rodar_um(arq, cfg, args)
    try:
        if erro is None:
            original = mover(arq, EDITADOS)
            registrar(f"{arq.name} ({preset}) -> {saida.relative_to(editar.RAIZ / 'saida')}")
            print(f"  O original foi guardado em entrada\\editados\\{original.name}")
        else:
            original = mover(arq, COM_ERRO)
            (COM_ERRO / f"{original.name}.erro.txt").write_text(
                f"Não foi possível editar '{arq.name}' ({preset}).\n\n{erro}\n\n"
                "Para tentar de novo, coloque o vídeo de volta na pasta entrada.\n", encoding="utf-8")
            registrar(f"ERRO em {arq.name}: {erro.splitlines()[0]}")
            print("  O original foi para entrada\\com-erro\\ (o motivo está no .txt ao lado dele).")
    except OSError as e:
        vigia.ignorar.add(arq)  # sem isso ele seria editado de novo a cada olhada
        print(f"  Atenção: não consegui tirar '{arq.name}' da pasta ({e.strerror}). Ele não será "
              "editado de novo; pode tirá-lo de lá quando quiser.", flush=True)
    tocar_aviso(erro is None)


def main(argv: list[str] | None = None) -> int:
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(description="Edita sozinho os vídeos que caírem na pasta entrada/.")
    p.add_argument("--uma-vez", action="store_true", help="edita o que já está na pasta e sai")
    opcoes = p.parse_args(argv)

    try:
        conferir_ffmpeg()
        pastas = pastas_vigiadas(editar.carregar_presets())
    except ErroEdicao as e:
        print(f"ERRO: {e}")
        return 1
    trava = pegar_trava()  # noqa: F841 (fica aberta enquanto o programa roda)
    if trava is None:
        print("O painel do editor (ou outra janela) já está cuidando da pasta entrada.")
        return 1
    for pasta in pastas:
        pasta.mkdir(parents=True, exist_ok=True)

    print("=" * 64)
    print(" EDITOR AUTOMÁTICO LIGADO")
    print("=" * 64)
    print(f" Coloque os vídeos em:  {ENTRADA}")
    print("   direto na pasta       -> Reels")
    print("   na subpasta youtube   -> vídeo do YouTube + legenda + 3 Shorts + fotos")
    outras = [n for n in pastas.values() if n not in (PRESET_PADRAO, "youtube")]
    if outras:
        print(f"   nas subpastas {', '.join(outras)} -> esses formatos")
    print(f" Os vídeos prontos aparecem em:  {editar.RAIZ / 'saida'}")
    print(" Para desligar, feche esta janela.")
    print("=" * 64, flush=True)

    vigia = Vigia()
    ultimo_erro = None
    aguardando = True
    try:
        while True:
            if aguardando:
                print(f"\n[{agora()}] Aguardando vídeos...", flush=True)
                aguardando = False
            fila, presentes = [], 0
            for pasta, preset in pastas.items():
                arquivos = sorted(videos_na_pasta(pasta, vigia), key=lambda a: a[1])
                presentes += len(arquivos)
                fila += [(a, preset) for a, _ in arquivos if vigia.pronto(a)]
            for arq, preset in fila:
                try:
                    editar_um(arq, preset, vigia)
                    ultimo_erro = None
                except ErroEdicao as e:  # problema no presets.yaml: o vídeo espera na pasta
                    if str(e) != ultimo_erro:
                        print(f"\n[{agora()}] ERRO: {e}\n  O vídeo continua na pasta; "
                              "assim que o problema for corrigido ele será editado.", flush=True)
                        ultimo_erro = str(e)
                    if opcoes.uma_vez:
                        return 1
                    break
                except Exception as e:  # nunca derruba o editor por causa de um vídeo
                    vigia.ignorar.add(arq)
                    print(f"\n[{agora()}] ERRO inesperado com '{arq.name}': {e}\n"
                          "  Ele fica parado na pasta; os próximos vídeos seguem normalmente.", flush=True)
                finally:
                    vigia.esquecer(arq)
                aguardando = True
            if opcoes.uma_vez and presentes == 0:
                return 0
            time.sleep(INTERVALO)
    except KeyboardInterrupt:
        print("\nEditor automático desligado.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
