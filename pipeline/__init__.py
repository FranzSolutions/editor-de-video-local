"""Etapas do Editor de Vídeo Local. Cada módulo tem `executar(video, ctx) -> Path`:
recebe o caminho do vídeo da etapa anterior e devolve o caminho do novo arquivo.

Ordem em que rodam (editar.py):
  preparar -> audio_externo -> transcrever -> cortar_silencios -> formato -> legendas
  -> logo -> chamada_final -> trilha -> normalizar -> exportar

A trilha entra depois da chamada final para a música continuar por baixo dela e
terminar com fade-out junto com o vídeo.
"""
