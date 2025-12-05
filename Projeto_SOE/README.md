Projeto de Monitoramento de Estoque IoT

Este repositório contém o código fonte completo para o sistema de monitoramento de bancadas via QR Code.

Estrutura do Projeto

Backend/: Código do servidor (PC/Nuvem).

app.py: Servidor Flask principal.

templates/: Telas HTML (Login, Admin, Dashboard).

static/: Fotos e assets.

Firmware/: Código do Raspberry Pi (C++).

leitor_qr.cpp: Lógica principal.

leitor_qr.service: Serviço de inicialização automática.

Instalação

Servidor (Windows/Linux)

Instale Python.

pip install -r requirements.txt

python app.py

Raspberry Pi

Instale dependências: sudo apt install libopencv-dev libcurl4-openssl-dev libzbar-dev

Instale WiringPi.

Compile: g++ leitor_qr.cpp -o leitor $(pkg-config --cflags --libs opencv4) -lcurl -lzbar -lwiringPi -lpthread

Configure o serviço systemd se desejar boot automático.