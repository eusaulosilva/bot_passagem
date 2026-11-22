# Bot Monitor de Passagens — Brasília (DF) ➔ Santa Rita de Cássia (BA)

Bot em Python com **Playwright** (Chromium headless) para monitorar 24 horas por dia a liberação de passagens da viação **Rápido Federal (Grupo Guanabara)** para o dia **18/12/2026** em dois sites:

1. **Viaje Guanabara**
2. **ClickBus**

Quando passagens são detectadas com botões de compra visíveis e menção à viação Rápido Federal / Guanabara, o bot extrai os preços exibidos na página e envia um alerta formatado em HTML pelo **Telegram** com o link direto para compra.

---

## Estrutura do Projeto

```text
bot_passagem/
├── .github/
│   └── workflows/
│       └── monitorar.yml   # Execução automática a cada 10 minutos no GitHub Actions
├── bot.py                  # Script principal modular (verificar_guanabara e verificar_clickbus)
├── requirements.txt        # Dependências Python
├── .env.example            # Modelo de variáveis de ambiente
├── .gitignore              # Arquivos ignorados pelo Git
└── README.md               # Este guia
```

---

## Como Obter as Credenciais do Telegram

1. Abra o Telegram e procure por **@BotFather**.
2. Envie `/newbot`, siga as instruções e copie o **Token** gerado (`TELEGRAM_TOKEN`).
3. Inicie uma conversa com o seu novo bot enviando qualquer mensagem (ex.: `/start`).
4. Para descobrir seu `TELEGRAM_CHAT_ID`, acesse no navegador:
   `https://api.telegram.org/botSEU_TOKEN_AQUI/getUpdates`
   e copie o campo `"chat": {"id": 123456789}`.

---

## Como Testar Localmente

### 1. Criar e ativar o ambiente virtual

No Windows (PowerShell):

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

No Linux / macOS:

```bash
python3 -m venv venv
source venv/bin/activate
```

### 2. Instalar dependências e o navegador Chromium

```bash
pip install -r requirements.txt
playwright install chromium
```

### 3. Configurar o arquivo `.env`

Copie o arquivo de exemplo e preencha suas credenciais:

```powershell
Copy-Item .env.example .env
```

Edite o `.env` informando `TELEGRAM_TOKEN` e `TELEGRAM_CHAT_ID`.
- Deixe `MODO_LOOP_LOCAL=false` para executar apenas **uma verificação** e encerrar.
- Mude para `MODO_LOOP_LOCAL=true` se quiser deixar o script rodando em **loop contínuo** no seu computador (ajuste o tempo com `INTERVALO_SEGUNDOS=300` para 5 minutos, por exemplo).
- A proteção anti-spam (`NOTIFICAR_SEMPRE=false`) salva o estado em `estado_monitoramento.json` e envia alerta no Telegram apenas quando a passagem abre pela primeira vez, volta a ficar disponível ou muda de preço (também funciona entre execuções no GitHub Actions via cache).

### 4. Executar o bot

```bash
python bot.py
```

---

## Como Ativar o Monitoramento 24h no GitHub Actions

1. Suba este projeto para um repositório no **GitHub** (recomendado repositório privado ou público conforme sua preferência).
2. No repositório do GitHub, acesse **Settings** ➔ **Secrets and variables** ➔ **Actions**.
3. Clique em **New repository secret** e cadastre os seguintes secrets:
   - `TELEGRAM_TOKEN` *(obrigatório)*: token do seu bot no Telegram.
   - `TELEGRAM_CHAT_ID` *(obrigatório)*: seu Chat ID no Telegram.
   - `URL_GUANABARA` *(opcional — se omitido, usa a URL padrão de 18/12/2026)*.
   - `URL_CLICKBUS` *(opcional — se omitido, usa a URL padrão de 18/12/2026)*.
4. Acesse a aba **Actions**, selecione o workflow **Monitorar Passagens de Ônibus** e clique em **Run workflow** para fazer o primeiro teste manual.
5. A partir daí, o agendamento `*/10 * * * *` executará o bot automaticamente a cada 10 minutos.

> **Nota:** O agendador cron do GitHub Actions pode sofrer pequenas variações de atraso em horários de pico da plataforma.
