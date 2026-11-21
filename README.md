e2player Windows/Linux - player de TV ao estilo MAG (Infomir) para streaming de boxes Enigma2.

  * grupos = bouquets da box (+ Favoritos e Todos os canais)
  * lista de canais com programa atual e barra de progresso
  * pré-visualização do vídeo ao lado da lista, como no portal MAG
  * barra de informação (agora / a seguir) ao mudar de canal
  * guia EPG completo por canal
  * picons da box, favoritos, zapping por número, canal anterior
  * o e2proxy corre embutido: todos os canais passam pela correção da PMT

Requisitos (Windows ou Linux):
  - Python 3.8+ com Tkinter  (no Linux: sudo apt install python3-tk)
  - VLC instalado (no Windows, VLC de 64 bits se o Python for de 64 bits)
  - pip install python-vlc
  - opcional: pip install pillow   (picons com melhor qualidade)
  - e2proxy.py na mesma pasta

Uso:
  python3 e2player.py --box 192.168.1.65 [--user root --password xxx]
  (a configuração fica guardada em ~/.e2player.json; nas vezes seguintes
   basta  python3 e2player.py)

Teclas (ecrã inteiro):
  Cima / Baixo, PgUp / PgDn   canal seguinte / anterior
  OK (Enter)                  abrir a lista de canais
  Esquerda / Direita, + / -   volume
  0-9                         zapping por número
  Backspace                   canal anterior
  I  info    E / G  guia EPG    F  favorito    M  mute
  A  faixa de áudio    S  legendas    F11  janela/ecrã inteiro
  Ctrl+Q  sair

Teclas (lista):
  Cima / Baixo, PgUp / PgDn, Home / End   mover
  Esquerda / Direita          grupos <-> canais
  OK                          1.º OK reproduz na pré-visualização,
                              2.º OK passa a ecrã inteiro
  E / G  guia do canal    F  favorito    Esc  voltar ao vídeo


<img width="1920" height="1079" alt="channel_list_with_preview" src="https://github.com/user-attachments/assets/722a33cc-27cb-45d5-9067-de2902c247c4" />

<img width="1920" height="1079" alt="channel_streaming" src="https://github.com/user-attachments/assets/0ef1e0be-34e3-4d27-9923-dad6441b05db" />
