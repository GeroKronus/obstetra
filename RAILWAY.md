# Deploy no Railway

Este projeto é composto por **3 serviços** no Railway. Vai criar cada um no mesmo **projeto**.

## 1. PostgreSQL

No dashboard do Railway:
- `+ New` → `Database` → `Add PostgreSQL`
- Railway provisiona automaticamente. Anote a variável `DATABASE_URL` que ele expõe — a gente vai usar só uma parte dela.

## 2. Evolution API

- `+ New` → `Empty Service` (ou `Deploy from Docker Image`)
- Em **Settings → Source**: usar imagem Docker `atendai/evolution-api:v2.1.1`
- Em **Settings → Networking**: expor a porta `8080` publicamente (você vai precisar acessar `/manager` pra escanear o QR code).
- Em **Variables**, cole:

```
AUTHENTICATION_API_KEY=<gere-uma-chave-forte: openssl rand -hex 32>
SERVER_URL=https://${{RAILWAY_PUBLIC_DOMAIN}}
DEL_INSTANCE=false
DATABASE_ENABLED=true
DATABASE_PROVIDER=postgresql
DATABASE_CONNECTION_URI=${{Postgres.DATABASE_URL}}
DATABASE_CONNECTION_CLIENT_NAME=evolution_obstetra
DATABASE_SAVE_DATA_INSTANCE=true
DATABASE_SAVE_DATA_NEW_MESSAGE=true
DATABASE_SAVE_MESSAGE_UPDATE=true
DATABASE_SAVE_DATA_CONTACTS=true
DATABASE_SAVE_DATA_CHATS=true
CACHE_REDIS_ENABLED=false
CACHE_LOCAL_ENABLED=true
WEBHOOK_GLOBAL_URL=https://${{backend.RAILWAY_PUBLIC_DOMAIN}}/webhook
WEBHOOK_GLOBAL_ENABLED=true
WEBHOOK_GLOBAL_WEBHOOK_BY_EVENTS=false
WEBHOOK_EVENTS_MESSAGES_UPSERT=true
WEBHOOK_EVENTS_CONNECTION_UPDATE=true
CONFIG_SESSION_PHONE_CLIENT=Obstetra
CONFIG_SESSION_PHONE_NAME=Chrome
QRCODE_LIMIT=10
LANGUAGE=pt-BR
```

> **Importante**: troque `backend` na `WEBHOOK_GLOBAL_URL` pelo nome exato do seu serviço de backend (abaixo).

- Em **Settings → Volumes**: adicione um volume montado em `/evolution/instances` (senão a sessão do WhatsApp some em cada redeploy).

## 3. Backend

- `+ New` → `GitHub Repo` → selecione este repositório
- Em **Settings → Root Directory**: `backend`
- Em **Settings → Networking**: expor a porta `8000` publicamente.
- Em **Variables**:

```
ANTHROPIC_API_KEY=sk-ant-...
EVOLUTION_API_URL=http://${{evolution-api.RAILWAY_PRIVATE_DOMAIN}}:8080
EVOLUTION_API_KEY=<mesma chave do serviço acima>
EVOLUTION_INSTANCE_NAME=obstetra
DOCTOR_PHONE_NUMBER=55XXXXXXXXXXX
DOCTOR_NAME=Dra. Leiza
DATABASE_URL=sqlite:////data/obstetra.db
LOG_LEVEL=INFO
```

- Em **Settings → Volumes**: adicione um volume montado em `/data` (banco SQLite do backend).

> Substitua `evolution-api` na `EVOLUTION_API_URL` pelo nome exato do serviço da Evolution.

## 4. Parear o WhatsApp

1. Abra a URL pública da Evolution API: `https://<evolution>.railway.app/manager`
2. Faça login com a `AUTHENTICATION_API_KEY`.
3. Crie uma instância com o nome `obstetra` (mesmo valor de `EVOLUTION_INSTANCE_NAME`).
4. Escaneie o QR code com o celular que vai ser o número do bot.
5. Mande uma mensagem pro número a partir de outro celular — deve cair no webhook e o bot responde.

## Troubleshooting

- **Bot não responde**: verifique os logs do serviço `backend`. O mais comum é `ANTHROPIC_API_KEY` inválida ou o `WEBHOOK_GLOBAL_URL` apontando pro serviço errado.
- **Sessão Baileys perdida após redeploy**: volume não foi montado em `/evolution/instances`.
- **Doutora não recebe escaladas**: `DOCTOR_PHONE_NUMBER` precisa estar no formato internacional sem `+` (ex: `5511988887777`).
