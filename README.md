# Painel Mazza Broker

Painel web multiusuário para controlar o robô de extração de leads.

## Instalar

```bash
pip install -r requirements.txt
```

Se o Playwright ainda não tiver navegadores instalados nesta máquina:

```bash
python -m playwright install
```

## Rodar

```bash
uvicorn app.main:app --port 8000
```

Acesse `http://localhost:8000`.

Por padrão, fora do Docker o sistema roda em `MODO=local`: o Playwright abre janelas normais na máquina local, sem Xvfb/noVNC.

## Usuários

Os usuários ficam em `config/atendentes.json` (**não versionado** — veja `config/atendentes.example.json` para o formato). Cada atendente tem seu próprio `planilha_id`, usado para montar a URL do Google Sheets durante a execução, e uma senha com hash bcrypt (gerada pelo utilitário abaixo).

## Gerar hash bcrypt

Use o utilitário abaixo para criar o hash de uma nova senha:

```bash
python scripts/gerar_senha.py
```

Copie o hash gerado para o campo `senha_hash` em `config/atendentes.json`.

## Execução do robô

- O botão Iniciar dispara `POST /api/extracao/iniciar`.
- O botão Parar dispara `POST /api/extracao/parar`.
- Logs ao vivo chegam por `/ws/logs` e também são salvos em `logs/{atendente_id}_{timestamp}.log`.
- Perfis persistentes ficam em `perfis/{atendente_id}/sessao_painel` e `perfis/{atendente_id}/sessao_whatsapp`.
- Na primeira execução do `kaoe`, as pastas legadas `sessao_painel` e `sessao_whatsapp` são copiadas para `perfis/kaoe/`.
- A escrita na planilha usa a API do Google Sheets com `credenciais/google_service_account.json`.
- Compartilhe cada planilha com o `client_email` da service account.

Defina `APP_SECRET_KEY` no ambiente antes de usar fora do desenvolvimento local.

## Deploy em VPS / EasyPanel com Docker

O projeto inclui `Dockerfile` e `docker-compose.yml` para rodar em VPS com:

- FastAPI na porta `8080`
- Xvfb para telas virtuais por atendente e por serviÃ§o
- `x11vnc` + `websockify/noVNC` nas portas `6901-6920`
- Chromium instalado pelo Playwright

### ConfiguraÃ§Ã£o dos atendentes

Cada atendente em `config/atendentes.json` deve ter:

```json
{
  "display": ":101",
  "vnc_port": 6901,
  "display_painel": ":101",
  "vnc_port_painel": 6901,
  "display_whatsapp": ":111",
  "vnc_port_whatsapp": 6911,
  "vnc_senha": "senha-vnc-do-atendente"
}
```

Exemplo usado no projeto:

- `kaoe`: Painel `:101`/`6901`, WhatsApp `:111`/`6911`
- `pessoa2`: Painel `:102`/`6902`, WhatsApp `:112`/`6912`

Em `MODO=vps`, quando uma execuÃ§Ã£o comeÃ§a, o runner garante os dois displays do atendente, sobe Xvfb/openbox/x11vnc/websockify se ainda nÃ£o estiverem ativos, e abre o Chrome do Painel em `display_painel` e o Chrome do WhatsApp em `display_whatsapp`.

### Teste local com Docker Compose

```bash
docker compose up --build
```

Acesse:

```text
http://localhost:8080
```

Volumes persistentes mapeados:

- `./config:/app/config`
- `./perfis:/app/perfis`
- `./logs:/app/logs`
- `./credenciais:/app/credenciais`

Coloque `google_service_account.json` em `credenciais/` antes de usar a planilha via API.

### EasyPanel

1. Crie um app a partir do repositÃ³rio GitHub ou envie os arquivos do projeto.
2. Configure o build usando o `Dockerfile`.
3. Exponha a porta HTTP `8080`.
4. Defina variÃ¡veis de ambiente:

```text
MODO=vps
APP_PORT=8080
APP_SECRET_KEY=uma-chave-forte
WHATSAPP_TOKEN=token-da-cloud-api
```

5. Configure volumes persistentes para:

```text
/app/config
/app/perfis
/app/logs
/app/credenciais
```

6. Se for acessar noVNC por portas diretas, exponha tambÃ©m `6901-6920`. O painel tambÃ©m disponibiliza proxy autenticado em `/vnc/{atendente}/{servico}/...`, validando a sessÃ£o para impedir que um usuÃ¡rio acesse o display de outro.

## NotificaÃ§Ã£o WhatsApp ao finalizar

Configure `config/whatsapp_notify.json` com o `phone_number_id` da Cloud API:

```json
{
  "phone_number_id": "...",
  "token_env": "WHATSAPP_TOKEN"
}
```

O token deve ficar somente na variÃ¡vel de ambiente `WHATSAPP_TOKEN`.

Em `config/atendentes.json`, defina por atendente:

```json
"notificar_whatsapp": "5541XXXXXXXXX"
```

Use `null` para desativar. Mensagem livre da Cloud API depende da janela de 24h; se nÃ£o chegar, envie um "oi" para o nÃºmero da Mazza e rode novamente.
