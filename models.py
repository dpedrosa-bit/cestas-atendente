"""
models.py - Modelos SQLAlchemy do cestas-atendente

Compartilha a instância Postgres com cestas-company. Tabelas usam prefixo
`atendente_` para isolamento lógico.

Tabelas:
    atendente_sessions  — uma sessão por número de WhatsApp (TTL 24h por padrão)
    atendente_messages  — log completo de mensagens (user/assistant/tool)
    atendente_handoff   — escalações para atendente humano
"""
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.dialects.postgresql import JSONB

db = SQLAlchemy()


class AtendenteSession(db.Model):
    __tablename__ = 'atendente_sessions'

    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(32), nullable=False, index=True)
    # active | handoff | closed | expired
    status = db.Column(db.String(16), nullable=False, default='active', index=True)
    # Identidade do cliente (preenchido após primeiro lookup com sucesso)
    customer_id = db.Column(db.String(64), nullable=True)
    customer_name = db.Column(db.String(255), nullable=True)
    # Metadados livres (loja, tags, etc)
    meta = db.Column(JSONB, nullable=False, default=dict)
    # Contadores agregados (atualizados a cada turno)
    turn_count = db.Column(db.Integer, nullable=False, default=0)
    tokens_input_total = db.Column(db.Integer, nullable=False, default=0)
    tokens_output_total = db.Column(db.Integer, nullable=False, default=0)
    cache_read_total = db.Column(db.Integer, nullable=False, default=0)
    cache_write_total = db.Column(db.Integer, nullable=False, default=0)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_message_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    messages = db.relationship('AtendenteMessage', backref='session', lazy='dynamic',
                                cascade='all, delete-orphan')


class AtendenteMessage(db.Model):
    __tablename__ = 'atendente_messages'

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('atendente_sessions.id', ondelete='CASCADE'),
                            nullable=False, index=True)
    # user | assistant | tool
    role = db.Column(db.String(16), nullable=False)
    # Para mensagens "user" e "assistant": texto direto.
    # Para mensagens "tool": resultado da ferramenta serializado em JSON.
    content = db.Column(db.Text, nullable=False, default='')
    # Quando assistant chama ferramentas, registramos os tool_use blocks aqui
    # (lista de dicts com id, name, input). Quando role=tool, registramos o
    # tool_use_id correspondente.
    tool_calls = db.Column(JSONB, nullable=True)
    # Métricas Anthropic (apenas para mensagens assistant)
    model = db.Column(db.String(64), nullable=True)
    tokens_input = db.Column(db.Integer, nullable=True)
    tokens_output = db.Column(db.Integer, nullable=True)
    cache_read = db.Column(db.Integer, nullable=True)
    cache_write = db.Column(db.Integer, nullable=True)
    # Identificadores externos (msg id da Z-API)
    external_id = db.Column(db.String(128), nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)


class AtendenteHandoff(db.Model):
    __tablename__ = 'atendente_handoff'

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('atendente_sessions.id', ondelete='CASCADE'),
                            nullable=False, index=True)
    # Motivo curto (ex: "cliente pediu humano", "ia sem confiança", "valor alto")
    reason = db.Column(db.String(255), nullable=False)
    # Resumo gerado pela IA do que aconteceu até aqui
    summary = db.Column(db.Text, nullable=True)
    # pending | taken | resolved
    status = db.Column(db.String(16), nullable=False, default='pending', index=True)
    assigned_to = db.Column(db.String(128), nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    resolved_at = db.Column(db.DateTime, nullable=True)


def init_db(app):
    """Cria tabelas do atendente no Postgres compartilhado.
    Chamado uma vez no startup do app."""
    if not app.config.get('SQLALCHEMY_DATABASE_URI'):
        print('[DB] DATABASE_URL nao configurado — cestas-atendente exige Postgres')
        return False
    try:
        with app.app_context():
            db.create_all()
        print('[DB] Tabelas atendente_* prontas')
        return True
    except Exception as e:
        print(f'[DB] Erro ao criar tabelas: {e}')
        return False
