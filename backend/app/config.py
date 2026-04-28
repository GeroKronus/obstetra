from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str
    anthropic_model: str = "claude-opus-4-7"
    # Relay agent (interpretacao de comandos da doutora) usa modelo mais leve —
    # nao tem raciocinio clinico, so NLU + composicao + roteamento de ferramentas.
    anthropic_relay_model: str = "claude-sonnet-4-6"

    evolution_api_url: str
    evolution_api_key: str
    evolution_instance_name: str = "obstetra"

    doctor_phone_number: str = ""
    doctor_name: str = "Dra. Leiza"

    database_url: str = "sqlite:///./data/obstetra.db"

    log_level: str = "INFO"

    # Vault (Obsidian + GitHub) — opcional; se vazio, o bot roda sem contexto da paciente
    vault_repo_url: str = ""
    vault_local_path: str = "/data/vault"
    vault_ssh_private_key: str = ""
    vault_git_user_name: str = "Obstetra Bot"
    vault_git_user_email: str = "obstetra-bot@noreply.local"
    # Quanto tempo (segundos) o cache local do vault é considerado fresco antes de um novo pull
    vault_pull_max_age_s: int = 30

    # Admin web (rota /admin) — usuário/senha pra secretária
    admin_user: str = ""
    admin_password: str = ""


settings = Settings()
