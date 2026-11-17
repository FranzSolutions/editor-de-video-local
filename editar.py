"""Editor de Vídeo Local: transforma um vídeo bruto em vídeo pronto para postar
(cortado, legendado, com trilha, logo e chamada final).

Exemplos:
  python editar.py entrada/video.mp4 --preset reels
  python editar.py entrada/video.mp4 --audio entrada/locucao.mp3 --trilha musica.mp3 --sem-legenda
  python editar.py entrada/ --preset stories          (processa a pasta inteira)
  python editar.py entrada/video.mp4 --so-transcrever  (gera a legenda para revisar antes)
  python editar.py entrada/aula.mp4 --preset youtube   (vídeo longo + legenda + 3 Shorts + fotos)

Para usar pela página (arrastar vídeos, revisar legenda, assistir): python painel.py
(ou dois cliques em "Abrir Editor.bat").
"""

from __future__ import annotations

import argparse
import copy
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Callable

import yaml

from pipeline import (audio_externo, capas, chamada_final, cortar_silencios, exportar, formato,
                      legenda_youtube, legendas, logo, normalizar, preparar, shorts, transcrever,
                      trilha)
from pipeline.contexto import FORMATOS, Contexto
from pipeline.util import (ErroEdicao, aviso, conferir_ffmpeg, dimensoes, duracao, ler_tempo,
                           marca_trecho, num, slug)

RAIZ = Path(__file__).resolve().parent
EXTENSOES_VIDEO = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".mts", ".3gp", ".wmv"}


# ---------------------------------------------------------------- presets

def mesclar(base: dict, extra: dict) -> dict:
    resultado = copy.deepcopy(base)
    for chave, valor in (extra or {}).items():
        if isinstance(valor, dict) and isinstance(resultado.get(chave), dict):
            resultado[chave] = mesclar(resultado[chave], valor)
        else:
            resultado[chave] = copy.deepcopy(valor)
    return resultado


def carregar_presets() -> dict:
    """config/presets.yaml + config/ajustes.yaml (o que foi mudado pelo painel, por cima)."""
    presets: dict = {}
    for nome in ("presets.yaml", "ajustes.yaml"):
        arquivo = RAIZ / "config" / nome
        if not arquivo.exists():
            continue
        try:
            dados = yaml.safe_load(arquivo.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise ErroEdicao(f"Erro de digitação em config/{nome}:\n{e}")
        for preset, valores in dados.items():
            presets[preset] = mesclar(presets.get(preset, {}), valores or {})
    return presets


def montar_cfg(presets: dict, args) -> dict:
    nomes = [n for n in presets if n != "padrao"]
    if args.preset not in nomes:
        raise ErroEdicao(f"O preset '{args.preset}' não existe. Opções: {', '.join(nomes)}.")
    cfg = mesclar(presets.get("padrao", {}), presets[args.preset])

    if args.formato:
        cfg["formato"] = args.formato
    if cfg["formato"] not in FORMATOS:
        raise ErroEdicao(f"Formato '{cfg['formato']}' não existe. Opções: {', '.join(FORMATOS)}.")
    if args.sem_legenda:
        cfg["legendas"]["ativo"] = False
    if args.sem_cortes:
        cfg["cortes"]["ativo"] = False
    if args.remover_vicios:
        cfg["cortes"]["remover_vicios"] = True
    if args.pausa is not None:
        cfg["cortes"]["pausa_maxima"] = args.pausa
    if args.trilha:
        cfg["trilha"].update(ativo=True, arquivo=args.trilha)
    if args.sem_trilha:
        cfg["trilha"]["ativo"] = False
    if args.sem_logo:  # sem logo em lugar nenhum: nem no canto, nem na tela final
        cfg["logo"]["ativo"] = False
        cfg["chamada_final"]["logo"] = False
    if args.chamada:
        estilos = cfg["chamada_final"].get("linhas") or [{}]
        textos = [t.strip() for t in args.chamada.split("|") if t.strip()]
        cfg["chamada_final"]["linhas"] = [
            {**estilos[min(i, len(estilos) - 1)], "texto": t} for i, t in enumerate(textos)]
        cfg["chamada_final"]["ativo"] = True
    if args.sem_chamada:
        cfg["chamada_final"]["ativo"] = False
    if args.sem_normalizar:
        cfg["normalizar"]["ativo"] = False
    if args.modelo:
        cfg["transcricao"]["modelo"] = args.modelo
    if args.modo_audio:
        cfg["audio_externo"]["modo"] = args.modo_audio
    if args.shorts is not None:
        cfg.setdefault("shorts", {})["quantidade"] = args.shorts

    # os cortes contam amostras de áudio por quadro: 48000 / fps precisa dar número inteiro
    try:
        fps = float(cfg["fps"])
    except (TypeError, ValueError):
        fps = 0.0
    if not fps.is_integer() or fps <= 0 or 48000 % int(fps):
        raise ErroEdicao(f"fps inválido no preset ({cfg['fps']}). Use 24, 25, 30, 50 ou 60.")
    cfg["fps"] = int(fps)
    return cfg


# ---------------------------------------------------------------- entrada

def achar(caminho: str) -> Path:
    """Aceita caminho relativo à pasta atual ou à pasta do editor."""
    p = Path(caminho)
    if not p.exists() and not p.is_absolute() and (RAIZ / p).exists():
        p = RAIZ / p
    if not p.exists():
        raise ErroEdicao(f"Não encontrei '{caminho}'.")
    return p.resolve()


def listar_videos(alvos: list[str]) -> list[Path]:
    videos = []
    for alvo in alvos:
        p = achar(alvo)
        if p.is_dir():
            achados = sorted(f for f in p.iterdir() if f.suffix.lower() in EXTENSOES_VIDEO)
            if not achados:
                aviso(f"nenhum vídeo em {p}.")
            videos += achados
        else:
            videos.append(p)
    videos = list(dict.fromkeys(videos))  # o mesmo arquivo passado duas vezes

    # x.mp4 e x.mov iriam para o mesmo saida/x_<preset>.mp4
    vistos: dict[str, Path] = {}
    for v in videos:
        anterior = vistos.setdefault(v.stem.lower(), v)
        if anterior is not v:
            raise ErroEdicao(f"'{anterior.name}' e '{v.name}' têm o mesmo nome e o resultado de um "
                             "apagaria o do outro. Renomeie um deles.")
    return videos


# ---------------------------------------------------------------- execução

def montar_etapas(ctx: Contexto, args, revisando: bool = False) -> list[tuple[str, Callable, bool]]:
    """(título, função, muda_o_video). A transcrição não gera vídeo novo. Com revisão na
    linha do tempo, a etapa de corte sempre entra (a pessoa pode cortar trechos à mão)."""
    cfg = ctx.cfg
    cortes = cfg["cortes"]
    if args.so_transcrever and transcrever.cache_valido(ctx):
        return [("Transcrevendo a fala", transcrever.executar, False)]
    etapas = [("Preparando o vídeo", preparar.executar, True)]
    if ctx.audio_externo:
        etapas.append(("Juntando o áudio externo", audio_externo.executar, True))
    if (args.so_transcrever or cfg["legendas"]["ativo"] or cortes["ativo"] or cortes["remover_vicios"]
            or cfg.get("legenda_youtube") or quantidade_shorts(cfg)):
        etapas.append(("Transcrevendo a fala", transcrever.executar, False))
    if args.so_transcrever:
        return etapas
    if cortes["ativo"] or cortes["remover_vicios"] or revisando:
        titulo = {(True, False): "Cortando pausas", (True, True): "Cortando pausas e vícios",
                  (False, True): "Cortando vícios", (False, False): "Aplicando os cortes"}[
            (bool(cortes["ativo"]), bool(cortes["remover_vicios"]))]
        etapas.append((titulo, cortar_silencios.executar, True))
    W, H = ctx.tamanho
    etapas.append((f"Ajustando o formato ({cfg['formato']} {W}x{H})", formato.executar, True))
    if cfg["legendas"]["ativo"]:
        etapas.append(("Queimando as legendas", legendas.executar, True))
    if cfg["logo"]["ativo"]:
        etapas.append(("Colocando o logo", logo.executar, True))
    if cfg["chamada_final"]["ativo"]:
        etapas.append(("Adicionando a chamada final", chamada_final.executar, True))
    if cfg["trilha"]["ativo"]:
        etapas.append(("Colocando a trilha", trilha.executar, True))
    if cfg["normalizar"]["ativo"]:
        etapas.append(("Normalizando o volume", normalizar.executar, True))
    etapas.append(("Exportando", exportar.executar, True))
    if cfg.get("legenda_youtube"):
        etapas.append(("Salvando a legenda para o YouTube", legenda_youtube.executar, False))
    if int(cfg.get("capas") or 0):
        etapas.append(("Separando fotos para a thumbnail", capas.executar, False))
    return etapas


def quantidade_shorts(cfg: dict) -> int:
    return int((cfg.get("shorts") or {}).get("quantidade") or 0)


def pastas_temp(original: Path, audio: Path | None, inicio: float | None, fim: float | None,
                preset: str) -> tuple[Path, Path]:
    """temp/<video>/ guarda a transcrição e a legenda de uma "linha do tempo" (vídeo +
    locução + trecho). Os intermediários de cada rodada ficam em temp/<video>/run-<preset>/,
    que é a única coisa apagada no fim."""
    nome = slug(original.stem)
    if audio:
        nome += "_" + slug(audio.stem)
    if inicio is not None or fim is not None:
        nome += "_" + marca_trecho(inicio, fim)
    temp = RAIZ / "temp" / nome
    return temp, temp / f"run-{slug(preset)}"


def revisar_legenda(ctx: Contexto, video: Path | None = None) -> None:
    srt = ctx.legenda_srt or ctx.temp / "legenda.srt"
    if not sys.stdin or not sys.stdin.isatty():
        aviso("não dá para esperar a revisão neste modo; seguindo com a legenda como está.")
        return
    print(f"      Abrindo a legenda no Bloco de Notas: {srt}")
    try:
        subprocess.Popen(["notepad.exe", str(srt)])
    except OSError:
        pass
    try:
        input("      Corrija o que precisar, SALVE o arquivo e aperte Enter aqui para continuar... ")
    except EOFError:
        return
    transcrever.carregar_legenda(ctx)


Andamento = Callable[[float, str], None]  # (0..1, "o que está fazendo")


def processar(original: Path, cfg: dict, args, pasta_saida: Path | None = None,
              nome_saida: str | None = None, ao_progredir: Andamento | None = None,
              revisor: Callable[[Contexto, Path], None] | None = None,
              protegidos: list[tuple[float, float]] | None = None) -> Contexto:
    audio = achar(args.audio) if args.audio else None
    legenda = achar(args.legenda) if args.legenda else None
    inicio = ler_tempo(args.inicio) if args.inicio else None
    fim = ler_tempo(args.fim) if args.fim else None
    temp, trabalho = pastas_temp(original, audio, inicio, fim, args.preset)
    for arq in (original, audio, legenda):
        if arq and arq.is_relative_to(trabalho):
            raise ErroEdicao(f"Tire '{arq.name}' de dentro de {trabalho.relative_to(RAIZ)} "
                             "(essa pasta é apagada a cada rodada).")
    shutil.rmtree(trabalho, ignore_errors=True)  # sobras de uma rodada anterior
    trabalho.mkdir(parents=True, exist_ok=True)

    ctx = Contexto(
        raiz=RAIZ, original=original, preset=args.preset, cfg=cfg, temp=temp, trabalho=trabalho,
        verbose=args.verbose, audio_externo=audio, legenda_srt=legenda, inicio=inicio, fim=fim,
        forcar_cpu=args.cpu, pasta_saida=pasta_saida, nome_saida=nome_saida,
        sobrescrever=not getattr(args, "vigiando", False), protegidos=list(protegidos or []),
    )
    try:
        etapas = montar_etapas(ctx, args, revisando=revisor is not None)
        video = original
        for n, (titulo, funcao, muda_video) in enumerate(etapas, 1):
            print(f"  [{n}/{len(etapas)}] {titulo}", flush=True)
            if ao_progredir:
                ao_progredir((n - 1) / len(etapas), titulo)
                ctx.progresso_etapa = (lambda f, n=n, t=titulo:
                                       ao_progredir((n - 1 + f) / len(etapas), t))
            resultado = funcao(video, ctx)
            if muda_video:
                video = resultado
            elif funcao is transcrever.executar and (revisor or args.revisar):
                (revisor or revisar_legenda)(ctx, video)
        if ao_progredir:
            ao_progredir(1.0, "Pronto")
    except BaseException as e:
        e.pasta_trabalho = trabalho  # para a mensagem de erro dizer onde olhar
        raise

    if not args.manter_temp:
        shutil.rmtree(trabalho, ignore_errors=True)
    if args.so_transcrever:
        srt = ctx.legenda_srt or temp / "legenda.srt"
        print(f"\n  Legenda para revisar: {srt}")
        print("  Abra no Bloco de Notas, corrija o texto (sem mexer nos números de tempo),")
        print("  salve e rode o mesmo comando sem --so-transcrever.")
    return ctx


def _pasta_livre(pasta: Path, sobrescrever: bool) -> Path:
    n, livre = 2, pasta
    while not sobrescrever and livre.exists() and any(livre.iterdir()):
        livre, n = pasta.with_name(f"{pasta.name} ({n})"), n + 1
    return livre


def gerar_shorts(ctx_longo: Contexto, args, quantidade: int,
                 ao_progredir: Andamento | None = None) -> list[Path]:
    """Corta sozinho os melhores trechos do vídeo longo e monta cada um como Short."""
    cfg_s = ctx_longo.cfg.get("shorts") or {}
    extra = [f"--{op}={getattr(args, op.replace('-', '_'))}" for op in ("audio", "modo-audio", "legenda", "modelo")
             if getattr(args, op.replace("-", "_"))]
    extra += [f"--{op}" for op in ("cpu", "verbose", "manter-temp", "remover-vicios")
              if getattr(args, op.replace("-", "_"))]
    if not args.legenda and not ctx_longo.inicio and not ctx_longo.fim:
        # os Shorts usam a mesma legenda do vídeo longo (inclusive a revisada)
        extra.append(f"--legenda={ctx_longo.temp / 'legenda.srt'}")
    args_s = criar_parser().parse_args([str(ctx_longo.original), "--preset", cfg_s.get("preset", "shorts"), *extra])
    cfg_short = montar_cfg(carregar_presets(), args_s)
    cortes = cfg_short["cortes"]
    trechos = shorts.escolher(ctx_longo.palavras, quantidade,
                              float(cfg_s.get("duracao_min", 30)), float(cfg_s.get("duracao_max", 60)),
                              float(cortes["pausa_maxima"]), float(cortes["margem"]))
    if not trechos:
        aviso("o vídeo é curto demais (ou tem pouca fala) para tirar Shorts.")
        return []

    pasta = ctx_longo.saida_final.parent
    deslocamento = ctx_longo.inicio or 0.0  # se o vídeo longo já era um trecho
    relatorio, prontos = [], []
    for k, t in enumerate(trechos, 1):
        inicio = max(0.0, t.inicio - 0.25) + deslocamento
        fim = t.fim + 0.4 + deslocamento
        print(f"\n  --- Short {k} de {len(trechos)}: {marca_trecho(inicio, fim)} "
              f"(~{num(t.duracao, 0)} s) ---", flush=True)
        args_k = criar_parser().parse_args([str(ctx_longo.original), "--preset", args_s.preset, *extra,
                                            "--inicio", f"{inicio:.3f}", "--fim", f"{fim:.3f}"])
        args_k.vigiando = getattr(args, "vigiando", False)
        andamento = None
        if ao_progredir:
            andamento = (lambda f, t, k=k: ao_progredir((k - 1 + f) / len(trechos),
                                                        f"Short {k} de {len(trechos)}: {t}"))
        ctx_k = processar(ctx_longo.original, cfg_short, args_k, pasta_saida=pasta,
                          nome_saida=f"{ctx_longo.original.stem}_short{k}", ao_progredir=andamento)
        prontos.append(ctx_k.saida_final)
        abertura = " ".join(t.texto.split()[:14])
        relatorio += [f"Short {k}: {ctx_k.saida_final.name}",
                      f"  Trecho do original: {marca_trecho(inicio, fim)} (~{num(t.duracao, 0)} s)",
                      f"  Por que foi escolhido: {', '.join(t.motivos) or 'melhor trecho disponível'}",
                      f"  Começa com: \"{abertura}...\"", ""]
    relatorio += ["Dica: no YouTube Studio, ligue cada Short ao vídeo completo",
                  "(campo \"Vídeo relacionado\")."]
    (pasta / "shorts.txt").write_text("\n".join(relatorio), encoding="utf-8")
    print(f"\n  {len(prontos)} Shorts prontos em {pasta.relative_to(RAIZ)} (detalhes em shorts.txt).")
    return prontos


def rodar_um(original: Path, cfg: dict, args, ao_progredir: Andamento | None = None,
             revisor: Callable[[Contexto, Path], None] | None = None,
             protegidos: list[tuple[float, float]] | None = None) -> tuple[Path | None, str | None]:
    """Edita um vídeo mostrando o andamento. Devolve (vídeo pronto, mensagem de erro)."""
    print(f"\n== {original.name}  [{args.preset}] ==", flush=True)
    inicio = time.time()
    try:
        n_shorts = 0 if args.so_transcrever else quantidade_shorts(cfg)
        pasta = None
        if not args.so_transcrever and (n_shorts or cfg.get("legenda_youtube") or int(cfg.get("capas") or 0)):
            # vários arquivos por vídeo (vídeo, legenda, Shorts, fotos): uma pasta só para ele
            pasta = _pasta_livre(RAIZ / "saida" / original.stem, not getattr(args, "vigiando", False))
            pasta.mkdir(parents=True, exist_ok=True)
        # na barra de progresso, o vídeo principal vale 65% quando ainda vêm Shorts depois
        peso = 0.65 if n_shorts else 1.0
        principal = (lambda f, t: ao_progredir(f * peso, t)) if ao_progredir else None
        ctx = processar(original, cfg, args, pasta_saida=pasta, ao_progredir=principal, revisor=revisor,
                        protegidos=protegidos)
        if ctx.saida_final:
            resumo(ctx.saida_final, cfg, time.time() - inicio)
            if n_shorts:
                resto = (lambda f, t: ao_progredir(peso + f * (1 - peso), t)) if ao_progredir else None
                gerar_shorts(ctx, args, n_shorts, ao_progredir=resto)
                print(f"\n  Tudo pronto em {num(time.time() - inicio, 0)} s.")
        return ctx.saida_final, None
    except ErroEdicao as e:
        print(f"\n  ERRO: {e}", file=sys.stderr, flush=True)
        _onde_olhar(e)
        return None, str(e)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as e:
        print(f"\n  ERRO inesperado: {e}", file=sys.stderr, flush=True)
        if args.verbose:
            traceback.print_exc()
        else:
            print("  Rode de novo com --verbose para ver os detalhes.")
        _onde_olhar(e)
        return None, f"Erro inesperado: {e}"


def resumo(saida: Path, cfg: dict, segundos: float) -> None:
    dur = duracao(saida)
    w, h = dimensoes(saida)
    mb = saida.stat().st_size / 1_048_576
    rel = saida.relative_to(RAIZ) if saida.is_relative_to(RAIZ) else saida
    print(f"\n  Pronto em {num(segundos, 0)} s -> {rel}")
    print(f"  ({num(dur)} s, {w}x{h}, {num(mb)} MB)")
    limite = float(cfg.get("duracao_maxima") or 0)
    if limite and dur > limite:
        aviso(f"o vídeo tem {num(dur)} s e o limite deste formato é {num(limite, 0)} s.")


class _Formatador(argparse.RawDescriptionHelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):
        return super().add_usage(usage, actions, groups, prefix="uso: ")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, f"erro: {message}\n")


def criar_parser() -> argparse.ArgumentParser:
    p = _Parser(
        prog="editar.py",
        description="Editor de Vídeo Local: corta, legenda, coloca trilha, logo e chamada final.",
        formatter_class=_Formatador,
        epilog="Exemplos:" + __doc__.split("Exemplos:", 1)[1],
        add_help=False,
    )
    p._positionals.title = "entrada"
    p._optionals.title = "opções"
    p.add_argument("-h", "--help", "--ajuda", action="help", help="mostra esta ajuda e sai")
    p.add_argument("alvos", nargs="*", metavar="VIDEO_OU_PASTA", help="vídeo(s) ou pasta com vídeos")
    p.add_argument("--preset", default="reels", help="reels, stories, feed, podcast... (padrão: reels)")
    p.add_argument("--formato", choices=list(FORMATOS), help="troca o formato do preset")
    g = p.add_argument_group("áudio")
    g.add_argument("--audio", help="locução/áudio externo para juntar ao vídeo")
    g.add_argument("--modo-audio", choices=["substituir", "mixar"], help="o que fazer com o som original")
    g.add_argument("--trilha", help="música de assets/trilhas/ (ou caminho); 'aleatoria' sorteia uma")
    g.add_argument("--sem-trilha", action="store_true", help="não coloca música")
    g.add_argument("--sem-normalizar", action="store_true", help="não mexe no volume final")
    g = p.add_argument_group("cortes e legenda")
    g.add_argument("--sem-cortes", action="store_true", help="não corta as pausas")
    g.add_argument("--pausa", type=float, help="corta pausas maiores que isto, em segundos (padrão 0.6)")
    g.add_argument("--remover-vicios", action="store_true", help='corta "hã", "é...", "né"... (lista no preset)')
    g.add_argument("--sem-legenda", action="store_true", help="não queima legenda")
    g.add_argument("--so-transcrever", action="store_true", help="só gera a legenda (.srt) para revisar")
    g.add_argument("--revisar", action="store_true", help="abre a legenda no Bloco de Notas antes de queimar")
    g.add_argument("--legenda", help="usa este .srt (do vídeo original inteiro) em vez da transcrição automática")
    g.add_argument("--modelo", help="modelo do Whisper: tiny, base, small, medium, large-v3")
    g.add_argument("--cpu", action="store_true", help="transcreve pelo processador, sem usar a placa de vídeo")
    g = p.add_argument_group("visual")
    g.add_argument("--sem-logo", action="store_true", help="sem logo (nem no canto, nem na tela final)")
    g.add_argument("--chamada", help='texto da chamada final; "|" separa as linhas: "Gostou do vídeo?|Siga para mais"')
    g.add_argument("--sem-chamada", action="store_true", help="sem tela final")
    g.add_argument("--shorts", type=int, metavar="N", help="corta sozinho N Shorts de 30 a 60 s do vídeo (0 = nenhum)")
    g = p.add_argument_group("outros")
    g.add_argument("--inicio", help="usa o vídeo a partir daqui (ex.: 1:23)")
    g.add_argument("--fim", help="usa o vídeo até aqui (ex.: 2:05)")
    g.add_argument("--manter-temp", action="store_true", help="não apaga os arquivos intermediários")
    g.add_argument("-v", "--verbose", action="store_true", help="mostra os comandos do ffmpeg")
    g.add_argument("--listar-presets", action="store_true", help="mostra os presets e sai")
    g.add_argument("--baixar-modelo", action="store_true", help="baixa o modelo de transcrição e sai")
    return p


def _onde_olhar(erro: BaseException) -> None:
    pasta = getattr(erro, "pasta_trabalho", None)
    if pasta and pasta.is_dir() and any(pasta.iterdir()):
        print(f"  Os arquivos intermediários ficaram em {pasta.relative_to(RAIZ)}")


def main(argv: list[str] | None = None) -> int:
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = criar_parser().parse_args(argv)

    try:
        presets = carregar_presets()
        if args.listar_presets:
            for nome, valores in presets.items():
                if nome != "padrao":
                    cfg = mesclar(presets.get("padrao", {}), valores)
                    W, H = FORMATOS.get(cfg["formato"], ("?", "?"))
                    print(f"  {nome:14} {cfg['formato']} {W}x{H}")
            return 0
        cfg = montar_cfg(presets, args)
        if args.baixar_modelo:
            caminho = transcrever.garantir_modelo(cfg["transcricao"]["modelo"], RAIZ)
            print(f"Modelo pronto: {caminho}")
            return 0
        if not args.alvos:
            criar_parser().print_help()
            return 2
        conferir_ffmpeg()
        videos = listar_videos(args.alvos)
        if len(videos) > 1 and (args.audio or args.legenda or args.inicio or args.fim):
            raise ErroEdicao("--audio, --legenda, --inicio e --fim só valem para um vídeo por vez.")
    except ErroEdicao as e:
        print(f"ERRO: {e}", file=sys.stderr)
        return 1

    falhas = []
    try:
        for original in videos:
            _, erro = rodar_um(original, cfg, args)
            if erro:
                falhas.append(original.name)
    except KeyboardInterrupt:
        print("\nInterrompido.")
        return 130

    if len(videos) > 1:
        print(f"\n{len(videos) - len(falhas)} de {len(videos)} vídeos prontos.")
    if falhas:
        print("Com erro: " + ", ".join(falhas), file=sys.stderr)
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
