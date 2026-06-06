import html
import hashlib
import hmac
import json
import os
import random
import re
import ssl
import uuid
import unicodedata
import urllib.error
import urllib.request
from difflib import SequenceMatcher
from typing import Any

import certifi
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Carrega variáveis de ambiente (prioriza .env no desenvolvimento local)
load_dotenv(override=True)

app = FastAPI(title="Avatar Guia POLI UPE")

# ==============================
# CONFIGURACAO PERSONALIZAVEL
# ==============================
# Para customizar prompts, locais e falas, edite o dicionario abaixo ou crie
# um arquivo "polia_config.json" na raiz do projeto com os mesmos campos.

POLIA_CONFIG_PADRAO: dict[str, Any] = {
    "persona_base": [
        "Voce e a Polia, avatar guia da POLI U-P-E.",
        "Fale em portugues do Brasil, de forma acolhedora, clara e objetiva.",
        "Prefira frases naturais e uteis para calouros.",
    ],
    "diretrizes_base": [
        "Nao use aspas nem emojis.",
        "Evite informacoes inventadas; quando faltar contexto, seja transparente.",
        "Quando mencionar UPE, diga U-P-E para soletrar a sigla.",
    ],
    "contexto_global_extra": [],
    "chat": {
        "objetivo": "Responder duvida geral do calouro de forma util e natural.",
        "regras": ["Responda com no maximo 3 frases.", "Seja direto e natural."],
        "fallback": "Tenta me perguntar por bloco ou sala, tipo B01, K06 ou Biblioteca, que eu te guio rapidinho.",
        "fora_contexto": "Eu so consigo falar sobre a POLI U-P-E e o campus. Pergunta por bloco, sala ou servico da POLI que eu te ajudo.",
    },
    "destino": {
        "objetivo": "Classificar a intencao da pergunta e inferir destino do campus.",
        "regras": [
            "Responda SOMENTE com JSON valido, sem texto fora do JSON.",
            "Use apenas um ID presente em destinos_validos.",
            "Se nao houver destino claro, use destino=null.",
            "Considere equivalencias como sala, classe, laboratorio, LIP, bloco, abreviacoes e pequenos erros de digitacao.",
        ],
        "saida_esperada": '{"destino": "<id_do_destino_ou_null>", "motivo": "<resumo_curto>"}',
    },
    "fala_bloco": {
        "objetivo": "Recepcionar o calouro com contexto real do bloco, de forma breve e util.",
        "regras": [
            "Crie 1 frase (ou 2 frases curtas) entre 12 e 24 palavras.",
            "Cite explicitamente o nome do local.",
            "Cada interacao deve soar nova; evite repetir estrutura e palavras.",
            "Mencione 1 destaque real do bloco.",
            "Se houver conhecimento_pratico, inclua 1 dica util ao calouro.",
            "Nao invente informacoes.",
        ],
    },
    # Para adicionar novos locais (exemplo):
    # "locais_extra": {
    #   "bloco x": {"x": 12.3, "y": 45.6, "salas": ["X01"], "dica": "..."}
    # }
    "locais_extra": {},
    # Para adicionar sinonimos extras (exemplo):
    # "sinonimos_extra": {"biblioteca": ["bibli", "acervo"]}
    "sinonimos_extra": {},
    "acessibilidade": {
        "objetivo": "Responder sobre rampas, escadas e elevadores da POLI com base nos pontos cadastrados.",
        "regras": [
            "Seja objetiva e clara.",
            "Quando houver bloco informado, cite o bloco.",
            "Se nao houver dado cadastrado, diga que ainda nao foi mapeado.",
        ],
        "fallback": "Todos os blocos possuem escadas, nem todos possuem elevadores, e as rampas estao sendo construídas gradativamente.",
        "geral": [
            "Todos os blocos possuem escadas.",
            "Nem todos os blocos possuem elevadores.",
            "As rampas estao sendo construídas gradativamente.",
        ],
        "rampa": [
            "As rampas estao sendo construídas gradativamente na POLI.",
        ],
        "escada": [
            "Todos os blocos possuem escadas.",
        ],
        "elevador": [
            "Nem todos os blocos possuem elevadores.",
        ],
    },
    "acessibilidade_locais": [],
}


def _merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    resultado = dict(base)
    for chave, valor in (override or {}).items():
        if isinstance(valor, dict) and isinstance(resultado.get(chave), dict):
            resultado[chave] = _merge_config(resultado[chave], valor)
        else:
            resultado[chave] = valor
    return resultado


def carregar_config_polia() -> dict[str, Any]:
    caminho = "polia_config.json"
    if not os.path.exists(caminho):
        return dict(POLIA_CONFIG_PADRAO)

    try:
        with open(caminho, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return dict(POLIA_CONFIG_PADRAO)
        return _merge_config(POLIA_CONFIG_PADRAO, data)
    except Exception as e:
        print(f"Aviso: falha ao ler {caminho}: {e}")
        return dict(POLIA_CONFIG_PADRAO)


POLIA_CONFIG = carregar_config_polia()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

EVENTOS_ARQUIVO = os.path.join(BASE_DIR, "eventos.json")
EVENTOS_EMAILS_ARQUIVO = os.path.join(BASE_DIR, "eventos_emails.txt")
CADASTRO_EMAILS_ARQUIVO = os.path.join(BASE_DIR, "cadastro_emails.txt")
DA_EVENTOS_ATIVO = os.getenv("DA_EVENTOS_ATIVO", "1").strip() != "0"


def resolver_diretorio_static() -> str:
    candidatos = ["static", "Static"]
    for pasta in candidatos:
        if os.path.isdir(pasta):
            return pasta
    os.makedirs("static", exist_ok=True)
    return "static"


STATIC_DIR = resolver_diretorio_static()
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def chave_ordem_arquivo(nome_arquivo: str) -> list[Any]:
    partes = re.split(r"(\d+)", nome_arquivo)
    chave: list[Any] = []
    for parte in partes:
        if parte.isdigit():
            chave.append(int(parte))
        else:
            chave.append(parte.lower())
    return chave


def localizar_pasta_frames_avatar() -> str | None:
    candidatos = [
        os.path.join(STATIC_DIR, "frames"),
        os.path.join(STATIC_DIR, "frames_avatar"),
        os.path.join(STATIC_DIR, "animacao"),
        os.path.join(STATIC_DIR, "animation"),
    ]
    for pasta in candidatos:
        if os.path.isdir(pasta):
            return pasta
    return None


def listar_frames_avatar() -> list[str]:
    pasta_frames = localizar_pasta_frames_avatar()
    if not pasta_frames:
        return []

    arquivos = [
        nome
        for nome in os.listdir(pasta_frames)
        if nome.lower().endswith((".png", ".gif", ".ppm", ".pgm"))
    ]
    arquivos.sort(key=chave_ordem_arquivo)
    return arquivos


def prefixar_frames(frames: list[str]) -> list[str]:
    prefixados: list[str] = []
    for nome in frames:
        if "/" in nome:
            prefixados.append(nome)
        else:
            prefixados.append(f"frames/{nome}")
    return prefixados


def listar_frames_apresentacao(limite: int | None = None) -> list[str]:
    frames = prefixar_frames(listar_frames_avatar())
    inicio = 0
    fim = 156
    if not frames:
        return []

    fim = min(fim, len(frames) - 1)
    apresentados = frames[inicio : fim + 1]
    if limite is None:
        return apresentados
    if limite <= 0:
        return []
    return apresentados[:limite]


def calcular_animacao_boca(texto: str, frames_disponiveis: list[str] | None = None) -> dict[str, Any]:
    texto_limpo = (texto or "").strip()
    palavras = max(1, len(re.findall(r"\b\w+\b", texto_limpo)))
    duracao_estimativa_ms = int(max(0.9, (palavras * 0.42) + (len(texto_limpo) * 0.004)) * 1000)

    frames = frames_disponiveis or []
    if len(frames) >= 2:
        inicio = 49
        fim = 146
        fim = min(fim, len(frames) - 1)
        inicio = min(inicio, fim)
        recorte = frames[inicio : fim + 1]
        total_frames = min(len(recorte), max(8, min(160, palavras * 4)))
        sequencia = recorte[:total_frames]
    else:
        base = [
            "frames/frame_0050.png",
            "frames/frame_0060.png",
            "frames/frame_0070.png",
            "frames/frame_0080.png",
            "frames/frame_0070.png",
            "frames/frame_0060.png",
        ]
        ciclos = max(1, min(12, (palavras + 2) // 3))
        sequencia = base * ciclos

    if len(sequencia) < 2:
        sequencia = ["frames/frame_0045.png"]

    intervalo_ms = int(duracao_estimativa_ms / max(1, len(sequencia)))
    intervalo_ms = max(33, min(180, intervalo_ms))

    return {
        "palavras": palavras,
        "duracao_estimativa_ms": duracao_estimativa_ms,
        "intervalo_ms": intervalo_ms,
        "frames_disponiveis": len(frames),
        "sequencia": sequencia[:160],
    }


def metadados_animacao_para_texto(texto: str) -> dict[str, Any]:
    frames = prefixar_frames(listar_frames_avatar())
    return {
        "frames": {
            "pasta": localizar_pasta_frames_avatar(),
            "total": len(frames),
            "arquivos": frames,
        },
        "boca": calcular_animacao_boca(texto, frames_disponiveis=frames),
    }

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_TTS_MODEL = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
OPENAI_TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-4o-mini")
OPENAI_TTS_VOICE = os.getenv("OPENAI_TTS_VOICE", "shimmer")
GEMINI_TEXT_MODEL = os.getenv("GEMINI_TEXT_MODEL", "gemini-2.0-flash")
OPENAI_TTS_INSTRUCTIONS = os.getenv(
    "OPENAI_TTS_INSTRUCTIONS",
    "Fale em portugues do Brasil, voz feminina jovem, levemente infantil, tom acolhedor e claro.",
)

DA_USER = "da"
DA_PASSWORD = "diretorio@academico"
DA_SECRET = os.getenv("DA_SECRET") or os.getenv("SECRET_KEY") or "dev-secret"
DA_COOKIE_NAME = "da_auth"

genai_client = None
if GOOGLE_API_KEY:
    try:
        import google.genai as genai_module

        genai_client = genai_module.Client(api_key=GOOGLE_API_KEY)
        print("Google AI (genai) configurado com sucesso!")
    except Exception as e:
        print(f"Aviso: erro ao inicializar Google AI: {e}")
else:
    print("Aviso: GOOGLE_API_KEY não configurada. Respostas em fallback local.")


def gerar_texto_openai(prompt: str, temperatura: float = 0.6, max_tokens: int = 220) -> str | None:
    if not OPENAI_API_KEY:
        return None

    prompt_limpo = (prompt or "").strip()
    if not prompt_limpo:
        return None

    payload = {
        "model": OPENAI_TEXT_MODEL,
        "messages": [
            {"role": "system", "content": "Você é a Polia, avatar guia da POLÍ U-P-E. Quando mencionar UPE, diga U-P-E."},
            {"role": "user", "content": prompt_limpo},
        ],
        "temperature": temperatura,
        "max_tokens": max_tokens,
    }

    request = urllib.request.Request(
        url="https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENAI_API_KEY}",
        },
    )

    try:
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(request, timeout=30, context=ssl_ctx) as response:
            data = json.loads(response.read().decode("utf-8"))
        choices = data.get("choices") or []
        if not choices:
            return None
        message = choices[0].get("message") or {}
        texto = (message.get("content") or "").strip()
        return texto or None
    except urllib.error.HTTPError as e:
        erro = e.read().decode("utf-8", errors="ignore")
        print(f"Erro OpenAI Chat HTTP {e.code}: {erro}")
        return None
    except Exception as e:
        print(f"Erro OpenAI Chat: {e}")
        return None

locais_campus = {
    "entrada": {
        "x": 36.76,
        "y": 70.66,
        "salas": ["Portão Principal","Portaria", "Acesso Benfica", "Lanches do Magaiver"],
        "dica": "Lembre o calouro de andar sempre com o comprovante de matrícula nos primeiros dias.",
    },
    "estacionamento": {
        "x": 22.67,
        "y": 63.62,
        "salas": ["Vagas de Carros", "Motos"],
        "dica": "As vagas são destinadas a servidores da Poli e para alunos de moto, alunos de carro não podem entrar.",
    },
    "lanchonete": {
        "x": 17.22,
        "y": 38.97,
        "salas": ["Lanchonete", "Mesas de Refeição"],
        "dica": "O point do intervalo. Corre pra pegar o salgado antes da fila crescer.",
    },
    "bloco a": {
        "x": 27.33,
        "y": 43.66,
        "salas": ["Térreo: A01, NAPSI, A2, A3, Pós-Graduação", "1º Andar: Auditório"],
        "dica": "No A tem o NAPSI para apoio psicológico e o Auditório para eventos; atrás dele fica uma lanchonete muito elogiada.",
    },
    "bloco h": {
        "x": 44.49,
        "y": 47.65,
        "salas": ["Térreo: LIP-03, Biblioteca"],
        "dica": "No H, o LIP-03 costuma ter Expressão Gráfica, Programação e Estrutura de Dados, e a Biblioteca ajuda muito na rotina.",
    },
    "bloco g": {
        "x": 57.44,
        "y": 44.6,
        "salas": ["LIP-07", "Poli Junior"],
        "dica": "No G tem a Polir Junior e o LIP-07, onde rolam várias aulas de programação",
    },
    "bloco f": {
        "x": 65.28,
        "y": 44.6,
        "salas": ["LIP-01", "LIP 02"],
        "dica": "No F tem vários LIPs e o DTI, onde você resolve email institucional e acesso das máquinas.",
    },
    "bloco b": {
        "x": 56.65,
        "y": 58.45,
        "salas": ["Térreo: B01 a B04, DA, Lab. de Química", "1º Andar: Divisão de Estágio, Escolaridade"],
        "dica": "No B ficam muitas aulas dos períodos iniciais e a Escolaridade, onde se resolve matrícula.",
    },
    "da": {
        "x": 71.31,
        "y": 54.23,
        "salas": ["Diretório Acadêmico", "Área de Vivência", "Praça do Dominó (em frente ao D.A)"],
        "dica": "É a base dos alunos. Em frente ao D.A fica a Praça do Dominó, ponto clássico de convivência.",
    },
    "bloco i/k": {
        "x": 52.67,
        "y": 34.74,
        "salas": [
            "Bloco I (1º ao 3º Andar)",
            "Bloco K (Labs de Robótica e Topografia)",
            "DATP",
            "Sala dos Professores",
            "Praça do Dominó (próxima ao bloco I/K)",
        ],
        "dica": "No I/K ficam o DATP e a sala dos professores; perto dali também está a Praça do Dominó.",
    },
    "bloco e": {
        "x": 72.9,
        "y": 39.67,
        "salas": ["CSEC", "sala de atos"],
        "dica": "Fica na parte superior do campus e costuma acontecer alguns eventos por aqui.",
    },
    "bloco d": {
        "x": 81.42,
        "y": 42.49,
        "salas": ["Lab. Avançado de Construção Civil", "Sala do Empreendedor", "Sala de coworking"],
        "dica": "Confere direitinho se tua atividade é no D ou no E.",
    },
    "bloco j": {
        "x": 80.06,
        "y": 61.97,
        "salas": ["Labs de Eletrotécnica","Laboratório de telecomunicações", "laboratório de máquinas elétricas", "laboratório de eletrônica","Laboratório de Mecatrônica"],
        "dica": "Paraíso pra quem curte elétrica. Atenção com equipamentos.",
    },
    "bloco c": {
        "x": 89.26,
        "y": 58.69,
        "salas": ["Laboratório de Física Experimental", "Labs de Computação", "PPGEC", "Corisco", "Space Maker", "Lab Stream"],
        "dica": "Esse bloco vai aparecer bastante na tua rotina de computação.",
    },
}

locais_extra = POLIA_CONFIG.get("locais_extra") or {}
if isinstance(locais_extra, dict):
    for nome, dados in locais_extra.items():
        if not isinstance(dados, dict):
            continue
        x = dados.get("x")
        y = dados.get("y")
        salas = dados.get("salas")
        if x is None or y is None or not isinstance(salas, list):
            continue
        chave = str(nome).lower().strip()
        locais_campus[chave] = {
            "x": x,
            "y": y,
            "salas": salas,
            "dica": dados.get("dica", ""),
        }


def aplicar_locais_override(overrides: dict[str, Any] | None = None) -> None:
    dados = overrides if overrides is not None else (POLIA_CONFIG.get("locais_override") or {})
    if not isinstance(dados, dict):
        return
    for nome, coords in dados.items():
        if nome not in locais_campus or not isinstance(coords, dict):
            continue
        x = coords.get("x")
        y = coords.get("y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            locais_campus[nome]["x"] = float(x)
            locais_campus[nome]["y"] = float(y)


aplicar_locais_override()

SALAS_IMPORTANTES_POR_LOCAL = {
    "bloco a": ["A01", "NAPSI", "Auditório"],
    "bloco b": ["B01", "Escolaridade", "Lab. de Química"],
    "bloco c": ["LIP-01", "Labs de Computação", "Laboratório de Física Experimental, Laboratório de engenharia da computação, ecomp, Labmaker"],
    "bloco d": ["Lab. Avançado de Construção Civil", "Sala do Empreendedor"],
    "bloco e": ["Laboratórios de Informática", "Salas de Aula"],
    "bloco f": ["Salas de Aula"],
    "bloco g": ["LIP-07", "DTI", "Coordenação"],
    "bloco h": ["Biblioteca", "LIP-03"],
    "bloco i/k": ["DATP", "Sala dos Professores", "Labs de Robótica"],
    "bloco j": ["Labs de Eletrotécnica", "Máquinas Elétricas", "Mecatrônica"],
}

CONHECIMENTO_PRATICO_POR_LOCAL = {
    "bloco a": [
        "NAPSI oferece apoio psicológico aos alunos.",
        "O Auditório recebe eventos importantes da POLI.",
        "Atrás do Bloco A fica uma lanchonete com lanches muito elogiados.",
    ],
    "bloco b": [
        "As aulas ficam na parte de baixo do Bloco B; em cima ficam a Escolaridade e a Divisao de Estagio.",
        "Na Escolaridade são resolvidas pendências de matrícula.",
        "Na Escolaridade, é importante tratar Diva com respeito e gentileza.",
    ],
    "bloco h": [
        "No LIP-03 costumam ocorrer aulas de Expressão Gráfica.",
        "No LIP-03 também aparecem aulas de Programação e Estrutura de Dados.",
        "A Biblioteca é ponto-chave para estudo e consulta.",
    ],
    "bloco g": [
        "O Bloco G concentra vários LIPs (laboratórios de informática).",
        "No LIP-07 costumam ocorrer aulas de Introdução à Programação.",
        "No DTI é possível resolver email institucional e acesso às máquinas.",
    ],
    "bloco i/k": [
        "No bloco I/K ficam o DATP e a sala dos professores.",
        "A Praça do Dominó fica próxima ao bloco I/K e em frente ao D.A.",
    ],
    "da": [
        "Em frente ao D.A fica a Praça do Dominó, ponto tradicional de convivência.",
    ],
}

EXTRA_ALIASES_POR_LOCAL = {
    "bloco c": [
        "f01-lip1",
        "f01 lip1",
        "laboratorio de ecomp",
        "ecomp",
        "laboratorio de engenharia da computacao",
        "lab ecomp",
    ],
}

ultimas_falas_por_bloco: dict[str, str] = {}
historico_fallback_por_bloco: dict[str, list[str]] = {}
historico_falas_ia_por_bloco: dict[str, list[str]] = {}

POLIA_PERSONA_BASE = list(POLIA_CONFIG.get("persona_base") or [])

POLIA_DIRETRIZES_BASE = list(POLIA_CONFIG.get("diretrizes_base") or [])


def carregar_contexto_global_polia() -> list[str]:
    blocos: list[str] = []
    extras_config = POLIA_CONFIG.get("contexto_global_extra") or []
    if isinstance(extras_config, list):
        blocos.extend([str(x).strip() for x in extras_config if str(x).strip()])
    extra_env = (os.getenv("POLIA_EXTRA_CONTEXT") or "").strip()
    if extra_env:
        blocos.append(extra_env)

    arquivo_contexto = "polia_contexto_extra.txt"
    if os.path.exists(arquivo_contexto):
        try:
            with open(arquivo_contexto, "r", encoding="utf-8") as f:
                texto = f.read().strip()
            if texto:
                blocos.append(texto)
        except Exception as e:
            print(f"Aviso: falha ao ler {arquivo_contexto}: {e}")
    return blocos


def serializar_contexto_extra(contexto_extra: Any) -> str:
    if not contexto_extra:
        return ""
    if isinstance(contexto_extra, str):
        return contexto_extra.strip()
    if isinstance(contexto_extra, (list, tuple, set)):
        partes = [str(x).strip() for x in contexto_extra if str(x).strip()]
        return "\n".join(partes)
    if isinstance(contexto_extra, dict):
        partes = []
        for k, v in contexto_extra.items():
            if v is None:
                continue
            valor = str(v).strip()
            if valor:
                partes.append(f"{k}: {valor}")
        return "\n".join(partes)
    return str(contexto_extra).strip()


CONTEXTO_GLOBAL_POLIA = carregar_contexto_global_polia()


def montar_prompt_polia(
    objetivo: str,
    regras: list[str] | None = None,
    dados: dict[str, Any] | None = None,
    contexto_extra: Any = None,
) -> str:
    linhas: list[str] = []
    linhas.extend(POLIA_PERSONA_BASE)
    linhas.append(f"Objetivo: {objetivo}")

    if CONTEXTO_GLOBAL_POLIA:
        linhas.append("Contexto extra global da Polia:")
        linhas.extend(CONTEXTO_GLOBAL_POLIA)

    extra_req = serializar_contexto_extra(contexto_extra)
    if extra_req:
        linhas.append("Contexto extra desta requisição:")
        linhas.append(extra_req)

    if dados:
        linhas.append("Dados úteis:")
        for chave, valor in dados.items():
            if valor is None:
                continue
            linhas.append(f"- {chave}: {valor}")

    linhas.append("Diretrizes:")
    for regra in POLIA_DIRETRIZES_BASE:
        linhas.append(f"- {regra}")
    for regra in (regras or []):
        linhas.append(f"- {regra}")
    return "\n".join(linhas)


def escolher_fala_fallback(destino_id: str, opcoes: list[str]) -> str:
    usadas = set(historico_fallback_por_bloco.get(destino_id, []))
    disponiveis = [frase for frase in opcoes if frase not in usadas]
    if not disponiveis:
        usadas = set()
        disponiveis = opcoes[:]

    frase = random.choice(disponiveis)
    usadas.add(frase)
    historico_fallback_por_bloco[destino_id] = list(usadas)
    return frase


def obter_resposta_fora_contexto() -> str:
    chat_cfg = POLIA_CONFIG.get("chat") or {}
    return (
        chat_cfg.get("fora_contexto")
        or "Eu so consigo falar sobre a POLI U-P-E e o campus. Pergunta por bloco, sala ou servico da POLI que eu te ajudo."
    )


def normalizar_texto(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto)
    texto = texto.encode("ascii", "ignore").decode("ascii")
    texto = texto.lower()
    texto = re.sub(r"[^a-z0-9\s]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto


def remover_zeros_a_esquerda(numero: str) -> str:
    numero_limpo = (numero or "").lstrip("0")
    return numero_limpo or "0"


def gerar_aliases_codigo(prefixo: str, numero: str) -> list[str]:
    prefixo = (prefixo or "").lower()
    numero_original = numero or ""
    numero_sem_zero = remover_zeros_a_esquerda(numero_original)

    if not prefixo or not numero_original:
        return []

    aliases = {
        f"{prefixo}{numero_original}",
        f"{prefixo}-{numero_original}",
        f"{prefixo} {numero_original}",
        f"{prefixo}{numero_sem_zero}",
        f"{prefixo}-{numero_sem_zero}",
        f"{prefixo} {numero_sem_zero}",
    }

    if prefixo == "lip":
        aliases.update(
            {
                f"laboratorio de informatica {numero_original}",
                f"laboratorio de informatica {numero_sem_zero}",
                f"lab de informatica {numero_original}",
                f"lab de informatica {numero_sem_zero}",
                f"lab info {numero_original}",
                f"lab info {numero_sem_zero}",
            }
        )

    return [normalizar_texto(a) for a in aliases if a]


def extrair_codigos_texto(texto: str) -> list[tuple[str, str]]:
    codigos: list[tuple[str, str]] = []
    vistos: set[tuple[str, str]] = set()
    for prefixo, numero in re.findall(r"\b([A-Za-z]{1,4})\s*[- ]?\s*(\d{1,3})\b", texto or ""):
        chave = (prefixo.lower(), numero)
        if chave in vistos:
            continue
        vistos.add(chave)
        codigos.append(chave)
    return codigos


def deduplicar_indice(indice: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    vistos: set[tuple[str, str]] = set()
    unico: list[tuple[str, str, str]] = []
    for termo, destino, label in indice:
        chave = (termo, destino)
        if chave in vistos:
            continue
        vistos.add(chave)
        unico.append((termo, destino, label))
    return unico


def extrair_salas_importantes(destino_id: str, dados_local: dict, limite: int = 3) -> list[str]:
    predefinidas = SALAS_IMPORTANTES_POR_LOCAL.get(destino_id, [])
    if predefinidas:
        return predefinidas[:limite]

    candidatos: list[str] = []
    for item in dados_local.get("salas", []):
        partes = [p.strip() for p in re.split(r"[,;:]", item) if p.strip()]
        for parte in partes:
            parte = re.sub(
                r"^(terreo|1o andar|2o andar|3o andar)\s*",
                "",
                normalizar_texto(parte),
                flags=re.IGNORECASE,
            ).strip()
            if len(parte) >= 3:
                candidatos.append(parte)

    unicos: list[str] = []
    vistos: set[str] = set()
    for candidato in candidatos:
        if candidato in vistos:
            continue
        vistos.add(candidato)
        unicos.append(candidato)
    return unicos[:limite]


def extrair_conhecimento_pratico(destino_id: str, limite: int = 2) -> list[str]:
    return CONHECIMENTO_PRATICO_POR_LOCAL.get(destino_id, [])[:limite]


def construir_indice_salas() -> list[tuple[str, str, str]]:
    indice: list[tuple[str, str, str]] = []
    for destino, dados in locais_campus.items():
        indice.append((normalizar_texto(destino), destino, destino.upper()))

        if destino.lower().startswith("bloco") and "/" in destino:
            sufixo = destino.lower().replace("bloco", "", 1)
            letras = re.findall(r"[a-z]", sufixo)
            for letra in letras:
                termo = normalizar_texto(f"bloco {letra}")
                if termo:
                    indice.append((termo, destino, f"Bloco {letra.upper()}"))

        for item in dados["salas"]:
            item_norm = normalizar_texto(item)
            if item_norm:
                indice.append((item_norm, destino, item))

            for pref1, num1, pref2, num2 in re.findall(
                r"\b([A-Za-z]{1,4})\s*[- ]?\s*(\d{1,3})\s*a\s*([A-Za-z]{0,4})\s*[- ]?\s*(\d{1,3})\b",
                item,
                flags=re.IGNORECASE,
            ):
                prefixo_inicio = pref1.lower()
                prefixo_fim = (pref2 or pref1).lower()
                if prefixo_inicio != prefixo_fim:
                    continue

                inicio = int(num1)
                fim = int(num2)
                if inicio > fim:
                    inicio, fim = fim, inicio
                if (fim - inicio) > 50:
                    continue

                for n in range(inicio, fim + 1):
                    numero_txt = str(n).zfill(max(len(num1), len(num2), 2))
                    label_codigo = f"{prefixo_inicio.upper()}-{numero_txt}"
                    for alias in gerar_aliases_codigo(prefixo_inicio, numero_txt):
                        indice.append((alias, destino, label_codigo))

            for parte in re.split(r"[,;:]", item):
                parte_limpa = parte.strip()
                parte_norm = normalizar_texto(parte_limpa)
                if len(parte_norm) >= 3:
                    indice.append((parte_norm, destino, parte_limpa))

            for prefixo, numero in extrair_codigos_texto(item):
                label_codigo = f"{prefixo.upper()}-{numero.zfill(2)}"
                for alias in gerar_aliases_codigo(prefixo, numero):
                    indice.append((alias, destino, label_codigo))

        extras = EXTRA_ALIASES_POR_LOCAL.get(destino.lower(), [])
        for alias in extras:
            alias_norm = normalizar_texto(alias)
            if alias_norm:
                indice.append((alias_norm, destino, alias))

    return deduplicar_indice(indice)


indice_salas = construir_indice_salas()


def adicionar_sinonimos() -> None:
    sinonimos = {
        "biblioteca": ["bib", "livros", "estudo", "estudar", "acervo"],
        "lanchonete": ["lanche", "comida", "restaurante", "cafe", "lanchar"],
        "portaria": ["magaiver", "lanches do magaiver", "lanche do magaiver", "lanchonete do magaiver"],
        "laboratorio": ["lab", "oficina", "pratica", "equipamento"],
        "eletrotecnica": ["eletro", "eletrica", "elet"],
        "robotica": ["robo"],
        "topografia": ["topo"],
        "computacao": ["computador", "programacao", "codigo"],
        "informatica": ["info", "computador", "pc"],
        "salas": ["sala", "classe", "room", "ambiente", "dependencia"],
        "salas de aula": ["aula", "aulas", "classe", "sala de aula", "room"],
        "laboratorios de informatica": [
            "lip",
            "laboratorio de informatica",
            "lab de informatica",
            "lab info",
            "informatica",
        ],
    }
    sinonimos_extra = POLIA_CONFIG.get("sinonimos_extra") or {}
    if isinstance(sinonimos_extra, dict):
        for termo, lista in sinonimos_extra.items():
            if isinstance(termo, str) and isinstance(lista, list):
                sinonimos[termo] = lista
    global indice_salas

    for termo_orig, sinonimos_list in sinonimos.items():
        termo_orig_norm = normalizar_texto(termo_orig)
        for destino, dados in locais_campus.items():
            salas_norm = normalizar_texto(" ".join(dados["salas"]))
            if termo_orig_norm in salas_norm:
                for sin in sinonimos_list:
                    indice_salas.append((normalizar_texto(sin), destino, sin.capitalize()))

    indice_salas = deduplicar_indice(indice_salas)


adicionar_sinonimos()


def construir_aliases_blocos() -> dict[str, str]:
    aliases: dict[str, str] = {}
    for destino in locais_campus.keys():
        destino_lower = destino.lower().strip()
        if not destino_lower.startswith("bloco"):
            continue

        sufixo = destino_lower.replace("bloco", "", 1)
        for parte in re.findall(r"[a-z]+", sufixo):
            letra = parte[:1]
            if letra and letra.isalpha():
                aliases[letra] = destino
    return aliases


aliases_blocos = construir_aliases_blocos()


def inferir_destino_por_codigo(codigo: str) -> str | None:
    letra = (codigo or "")[:1].lower()
    return aliases_blocos.get(letra)


def buscar_destino_por_sala(pergunta: str) -> dict[str, str] | None:
    pergunta_norm = normalizar_texto(pergunta)

    codigos = extrair_codigos_texto(pergunta)
    for prefixo, numero in codigos:
        for alias in gerar_aliases_codigo(prefixo, numero):
            for termo, destino, label in indice_salas:
                if termo == alias:
                    return {"destino": destino, "sala": label}

        destino_inferido = inferir_destino_por_codigo(prefixo)
        if destino_inferido:
            return {"destino": destino_inferido, "sala": f"{prefixo.upper()}-{numero.zfill(2)}"}

    melhor: tuple[int, str, str] | None = None
    for termo, destino, label in indice_salas:
        if termo and termo in pergunta_norm:
            score = len(termo)
            if not melhor or score > melhor[0]:
                melhor = (score, destino, label)

    if melhor:
        return {"destino": melhor[1], "sala": melhor[2]}

    tokens = [t for t in pergunta_norm.split() if len(t) >= 3]
    melhor_fuzzy: tuple[float, str, str] | None = None
    for termo, destino, label in indice_salas:
        if not termo or len(termo) < 3:
            continue

        score = 0.0
        sim_frase = SequenceMatcher(None, pergunta_norm, termo).ratio()
        score = max(score, sim_frase * 0.7)

        for tk in tokens:
            sim = SequenceMatcher(None, tk, termo).ratio()
            if tk in termo or termo in tk:
                sim = max(sim, 0.9)
            score = max(score, sim)

        if score >= 0.84 and (not melhor_fuzzy or score > melhor_fuzzy[0]):
            melhor_fuzzy = (score, destino, label)

    if melhor_fuzzy:
        return {"destino": melhor_fuzzy[1], "sala": melhor_fuzzy[2]}
    return None


def extrair_json(texto: str) -> dict | None:
    if not texto:
        return None

    texto = texto.strip()
    try:
        return json.loads(texto)
    except Exception:
        pass

    inicio = texto.find("{")
    fim = texto.rfind("}")
    if inicio >= 0 and fim > inicio:
        trecho = texto[inicio : fim + 1]
        try:
            return json.loads(trecho)
        except Exception:
            return None
    return None


def listar_mencoes_locais(pergunta: str) -> list[tuple[int, str, str]]:
    texto_norm = normalizar_texto(pergunta)
    if not texto_norm:
        return []

    achados: list[tuple[int, str, str]] = []
    for termo, destino, label in indice_salas:
        if not termo:
            continue
        padrao = rf"\b{re.escape(termo)}\b"
        for match in re.finditer(padrao, texto_norm):
            achados.append((match.start(), destino, label))

    achados.sort(key=lambda item: item[0])
    unicos: list[tuple[int, str, str]] = []
    vistos: set[str] = set()
    for pos, destino, label in achados:
        if destino in vistos:
            continue
        vistos.add(destino)
        unicos.append((pos, destino, label))
    return unicos


def inferir_rota_por_texto(pergunta: str) -> dict[str, str] | None:
    texto_norm = normalizar_texto(pergunta)
    mencoes = listar_mencoes_locais(pergunta)
    if not mencoes:
        return None

    tem_origem = re.search(r"\b(estou|to|tô|saindo|partindo|vindo)\b", texto_norm)
    tem_destino = re.search(r"\b(quero|preciso|vou|ir|indo|destino|ate|até|para|pra|pro)\b", texto_norm)

    if len(mencoes) >= 2 and (tem_origem or tem_destino):
        return {"origem": mencoes[0][1], "destino": mencoes[-1][1]}

    if tem_origem:
        return {"origem": mencoes[0][1]}

    return None


def _detectar_consulta_lanche(texto_norm: str) -> dict[str, bool]:
    return {
        "tem_lanche": bool(re.search(r"\b(lanche|lanchonete|lanchar|salgado|comida|cafe)\b", texto_norm)),
        "tem_magaiver": bool(re.search(r"\b(magaiver|portaria)\b", texto_norm)),
        "tem_bloco_a": bool(re.search(r"\b(bloco a|poli)\b", texto_norm)),
    }


async def inferir_destino_com_ia(pergunta: str, contexto_extra: Any = None) -> dict[str, str] | None:
    destinos_validos = list(locais_campus.keys())
    destino_cfg = POLIA_CONFIG.get("destino") or {}
    objetivo_destino = destino_cfg.get("objetivo") or "Classificar a intencao da pergunta e inferir destino do campus."
    regras_destino = destino_cfg.get("regras") or []
    saida_esperada = destino_cfg.get("saida_esperada") or '{"destino": "<id_do_destino_ou_null>", "motivo": "<resumo_curto>"}'
    prompt = montar_prompt_polia(
        objetivo=objetivo_destino,
        dados={
            "destinos_validos": ", ".join(destinos_validos),
            "pergunta": pergunta,
            "saida_esperada": saida_esperada,
        },
        regras=regras_destino,
        contexto_extra=contexto_extra,
    )

    try:
        bruto = None
        if OPENAI_API_KEY:
            bruto = gerar_texto_openai(prompt, temperatura=0.2, max_tokens=140)
        elif genai_client:
            response = genai_client.models.generate_content(model=GEMINI_TEXT_MODEL, contents=prompt)
            bruto = (response.text or "").strip()

        data = extrair_json(bruto or "")
        if not isinstance(data, dict):
            return None

        destino = data.get("destino")
        if isinstance(destino, str):
            destino = destino.lower().strip()
        else:
            destino = None

        if destino in locais_campus:
            return {"destino": destino, "motivo": str(data.get("motivo") or "")}
        return None
    except Exception as e:
        print(f"Erro inferindo destino com IA: {e}")
        return None


async def gerar_fala_com_ia(destino_id: str, dados_local: dict, contexto_extra: Any = None) -> str:
    falas_fixas = {
        "entrada": "Voce chegou na entrada principal, na Portaria. Se precisar, pergunte que eu te guio.",
        "estacionamento": "Voce chegou no estacionamento. As vagas sao para servidores e para alunos de moto.",
        "lanchonete": "A lanchonete fica atras do Bloco A, aquele predio rosa. E o point do intervalo.",
    }
    fala_fixa = falas_fixas.get(destino_id)
    if fala_fixa:
        return fala_fixa

    local = destino_id.upper()
    salas = ", ".join(dados_local["salas"])
    salas_importantes = extrair_salas_importantes(destino_id, dados_local)
    salas_importantes_txt = ", ".join(salas_importantes) if salas_importantes else "nao especificadas"
    conhecimento_pratico = extrair_conhecimento_pratico(destino_id)
    conhecimento_pratico_txt = " | ".join(conhecimento_pratico) if conhecimento_pratico else ""
    dica = dados_local.get("dica", "Aproveite a POLI!")
    fala_anterior = ultimas_falas_por_bloco.get(destino_id, "")
    historico = historico_falas_ia_por_bloco.get(destino_id, [])[-5:]

    estilo = random.choice([
        "acolhedor e engracado",
        "empolgado e camarada",
        "direto e motivador",
        "leve e descontraido",
    ])

    fala_cfg = POLIA_CONFIG.get("fala_bloco") or {}
    objetivo_fala = fala_cfg.get("objetivo") or "Recepcionar o calouro com contexto real do bloco, de forma breve e util."
    regras_fala = fala_cfg.get("regras") or []

    prompt = montar_prompt_polia(
        objetivo=objetivo_fala,
        dados={
            "estilo": estilo,
            "local": local,
            "salas": salas,
            "salas_importantes": salas_importantes_txt,
            "conhecimento_pratico": conhecimento_pratico_txt,
            "dica": dica,
        },
        regras=regras_fala,
        contexto_extra=contexto_extra,
    )

    if "Cite explicitamente o nome do local" in " ".join(regras_fala):
        pass
    else:
        prompt += f"\nRegra adicional: cite explicitamente o nome do local: {local}."

    if fala_anterior:
        prompt += f" A última fala usada nesse bloco foi: '{fala_anterior}'. Não repita essa fala."
    if historico:
        prompt += " Evite semelhança com estas últimas falas do bloco: " + " | ".join(historico)

    if not genai_client and not OPENAI_API_KEY:
        destaque_curto = salas_importantes[0] if salas_importantes else local
        extra_util = conhecimento_pratico[0] if conhecimento_pratico else ""
        fallback = [
            f"Você chegou ao {local}. O destaque daqui é {destaque_curto}. {extra_util or dica}",
            f"No {local}, vale passar em {destaque_curto}. {extra_util or dica}",
            f"Bem-vindo ao {local}: referência rápida é {destaque_curto}. {extra_util or dica}",
        ]
        frase = escolher_fala_fallback(destino_id, fallback)
        ultimas_falas_por_bloco[destino_id] = frase
        return frase

    def frase_valida(texto: str) -> bool:
        t = (texto or "").strip()
        if not t:
            return False
        palavras = [p for p in re.split(r"\s+", t) if p]
        if len(palavras) < 8:
            return False
        return local.lower() in t.lower()

    try:
        if OPENAI_API_KEY:
            frase = (gerar_texto_openai(prompt, temperatura=0.8, max_tokens=120) or "").strip()
        else:
            response = genai_client.models.generate_content(model=GEMINI_TEXT_MODEL, contents=prompt)
            frase = (response.text or "").strip()

        repetida = any(frase.lower() == h.lower() for h in historico) or (
            fala_anterior and frase.lower() == fala_anterior.lower()
        )
        if repetida or not frase_valida(frase):
            if OPENAI_API_KEY:
                frase2 = (
                    gerar_texto_openai(
                        prompt + " Gere outra versão mais contextual, citando o bloco e um destaque real.",
                        temperatura=0.8,
                        max_tokens=120,
                    )
                    or ""
                ).strip()
            else:
                response2 = genai_client.models.generate_content(
                    model=GEMINI_TEXT_MODEL,
                    contents=prompt + " Gere outra versão mais contextual, citando o bloco e um destaque real.",
                )
                frase2 = (response2.text or "").strip()
            if frase2:
                frase = frase2

        if not frase_valida(frase):
            destaque_curto = salas_importantes[0] if salas_importantes else local
            extra_util = conhecimento_pratico[0] if conhecimento_pratico else dica
            frase = f"Você chegou ao {local}. O destaque daqui é {destaque_curto}. {extra_util}"

        ultimas_falas_por_bloco[destino_id] = frase
        historico_falas_ia_por_bloco.setdefault(destino_id, []).append(frase)
        return frase
    except Exception as e:
        print(f"Erro na IA: {e}")
        destaque_curto = salas_importantes[0] if salas_importantes else local
        extra_util = conhecimento_pratico[0] if conhecimento_pratico else ""
        fallback = [
            f"Você chegou ao {local}. O destaque daqui é {destaque_curto}. {extra_util or dica}",
            f"No {local}, vale passar em {destaque_curto}. {extra_util or dica}",
            f"Bem-vindo ao {local}: referência rápida é {destaque_curto}. {extra_util or dica}",
        ]
        frase = escolher_fala_fallback(destino_id, fallback)
        ultimas_falas_por_bloco[destino_id] = frase
        return frase


class RequisicaoLocal(BaseModel):
    destino: str
    contexto_extra: dict[str, Any] | str | list[str] | None = None


class RequisicaoChat(BaseModel):
    pergunta: str
    contexto_extra: dict[str, Any] | str | list[str] | None = None


class RotasPayload(BaseModel):
    locais_override: dict[str, dict[str, float]] | None = None
    rotas_offset: dict[str, dict[str, float]] | None = None


class MarcadorAcessibilidadePayload(BaseModel):
    id: str | None = None
    tipo: str
    x: float
    y: float
    bloco: str | None = None
    texto: str | None = None
    rotulo: str | None = None


class AcessibilidadePayload(BaseModel):
    marcadores: list[MarcadorAcessibilidadePayload]


class RequisicaoTTS(BaseModel):
    texto: str


def descrever_localizacao_destino(destino_id: str) -> str:
    destino_norm = (destino_id or "").lower().strip()
    if destino_norm == "entrada":
        return "Fica na entrada principal, na Portaria."
    if destino_norm == "estacionamento":
        return "Fica no estacionamento, logo apos a entrada."
    if destino_norm == "lanchonete":
        return "Fica atrás do Bloco A, aquele prédio rosa."
    if destino_norm.startswith("bloco "):
        return f"Fica no {destino_norm.upper()}."
    if destino_norm == "da":
        return "Fica no D.A."
    return f"Fica em {destino_norm.upper()}."


def salvar_config_polia(nova_config: dict[str, Any]) -> None:
    with open("polia_config.json", "w", encoding="utf-8") as f:
        json.dump(nova_config, f, ensure_ascii=False, indent=2)


def carregar_eventos() -> list[dict[str, Any]]:
    if not os.path.exists(EVENTOS_ARQUIVO):
        return []
    try:
        with open(EVENTOS_ARQUIVO, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [e for e in data if isinstance(e, dict)]
    except Exception as e:
        print(f"Aviso: falha ao ler {EVENTOS_ARQUIVO}: {e}")
    return []


def salvar_eventos(eventos: list[dict[str, Any]]) -> None:
    with open(EVENTOS_ARQUIVO, "w", encoding="utf-8") as f:
        json.dump(eventos, f, ensure_ascii=False, indent=2)


def registrar_email_evento(email: str, titulo: str, blocos: list[str], sala: str, campus_inteiro: bool) -> None:
    linha = " | ".join(
        [
            f"email={email}",
            f"titulo={titulo}",
            f"blocos={', '.join(blocos) if blocos else ('campus inteiro' if campus_inteiro else '')}",
            f"sala={sala}",
        ]
    ).strip()
    with open(EVENTOS_EMAILS_ARQUIVO, "a", encoding="utf-8") as f:
        f.write(linha + "\n")


def registrar_email_cadastro(email: str) -> None:
    email_limpo = (email or "").strip()
    if not email_limpo:
        return
    with open(CADASTRO_EMAILS_ARQUIVO, "a", encoding="utf-8") as f:
        f.write(email_limpo + "\n")


ACESSIBILIDADE_TIPOS = {
    "rampa": {"rotulo": "Rampa", "icone": "↗", "cor": "#1f8b3b", "fundo": "#daf8df"},
    "escada": {"rotulo": "Escada", "icone": "🪜", "cor": "#8b5a2b", "fundo": "#f7e3cb"},
    "elevador": {"rotulo": "Elevador", "icone": "⇅", "cor": "#1f5aa8", "fundo": "#d9e9ff"},
}


def _montar_falas_acessibilidade_config(tipo: str) -> list[str]:
    cfg = POLIA_CONFIG.get("acessibilidade") or {}
    dados = cfg.get(tipo)
    falas: list[str] = []

    if isinstance(dados, str) and dados.strip():
        return [dados.strip()]
    if not isinstance(dados, list):
        return []

    for item in dados:
        if isinstance(item, str):
            txt = item.strip()
            if txt:
                falas.append(txt)
            continue
        if not isinstance(item, dict):
            continue
        bloco = str(item.get("bloco") or "").strip().upper()
        texto = str(item.get("texto") or item.get("local") or item.get("descricao") or item.get("fala") or "").strip()
        if bloco and texto:
            falas.append(f"{bloco} - {texto}")
        elif texto:
            falas.append(texto)
    return falas


def normalizar_tipo_acessibilidade(tipo: str) -> str:
    tipo_norm = normalizar_texto(tipo)
    if tipo_norm in ACESSIBILIDADE_TIPOS:
        return tipo_norm
    if "rampa" in tipo_norm:
        return "rampa"
    if "escada" in tipo_norm:
        return "escada"
    if "elevador" in tipo_norm:
        return "elevador"
    return "rampa"


def normalizar_marcador_acessibilidade(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    tipo = normalizar_tipo_acessibilidade(str(item.get("tipo") or ""))
    x = item.get("x")
    y = item.get("y")
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None
    bloco = str(item.get("bloco") or "").strip().lower()
    if bloco and bloco not in locais_campus:
        bloco = ""
    texto = str(item.get("texto") or "").strip()
    rotulo = str(item.get("rotulo") or "").strip() or ACESSIBILIDADE_TIPOS[tipo]["rotulo"]
    return {
        "id": str(item.get("id") or uuid.uuid4().hex),
        "tipo": tipo,
        "x": float(x),
        "y": float(y),
        "bloco": bloco,
        "texto": texto,
        "rotulo": rotulo,
    }


def carregar_acessibilidade() -> list[dict[str, Any]]:
    dados = POLIA_CONFIG.get("acessibilidade_locais") or []
    if not isinstance(dados, list):
        return []
    normalizados: list[dict[str, Any]] = []
    for item in dados:
        marcador = normalizar_marcador_acessibilidade(item)
        if marcador:
            normalizados.append(marcador)
    return normalizados


def salvar_acessibilidade(marcadores: list[dict[str, Any]]) -> None:
    config_atual = carregar_config_polia()
    config_atual["acessibilidade_locais"] = marcadores
    salvar_config_polia(config_atual)
    global POLIA_CONFIG
    POLIA_CONFIG = config_atual


def formatar_marcador_acessibilidade(item: dict[str, Any]) -> str:
    tipo = str(item.get("tipo") or "rampa").strip().lower()
    bloco = str(item.get("bloco") or "").strip().upper()
    texto = str(item.get("texto") or "").strip()
    base = ACESSIBILIDADE_TIPOS.get(tipo, ACESSIBILIDADE_TIPOS["rampa"])["rotulo"]
    partes = [base]
    if bloco:
        partes.append(f"no {bloco}")
    if texto:
        partes.append(texto)
    return " - ".join(partes)


def extrair_tipo_acessibilidade_pergunta(texto_norm: str) -> str | None:
    for tipo in ("elevador", "escada", "rampa"):
        if tipo in texto_norm:
            return tipo
    return None


def extrair_blocos_acessibilidade_pergunta(texto_norm: str) -> list[str]:
    blocos: list[str] = []

    for destino in locais_campus:
        nome_norm = normalizar_texto(destino)
        if nome_norm and len(nome_norm) > 2 and re.search(rf"\b{re.escape(nome_norm)}\b", texto_norm):
            blocos.append(destino)

    for match in re.finditer(r"\bbloco\s+([a-z])\b", texto_norm):
        bloco = aliases_blocos.get(match.group(1))
        if bloco:
            blocos.append(bloco)

    if "biblioteca" in texto_norm:
        blocos.append("biblioteca")

    return list(dict.fromkeys(blocos))


def responder_acessibilidade_por_bloco(
    tipo: str,
    blocos_pedidos: list[str],
    marcadores: list[dict[str, Any]],
    acess_cfg: dict[str, Any],
) -> str | None:
    if tipo == "elevador":
        rotulo_plural = "elevadores"
    elif tipo == "escada":
        rotulo_plural = "escadas"
    else:
        rotulo_plural = "rampas"

    if tipo == "escada":
        if blocos_pedidos:
            if len(blocos_pedidos) == 1:
                return f"Sim. O {blocos_pedidos[0].upper()} tem escadas."
            blocos_txt = ", ".join(bloco.upper() for bloco in blocos_pedidos)
            return f"Sim. Os blocos {blocos_txt} têm escadas."
        geral = acess_cfg.get("escada") or []
        if isinstance(geral, list):
            for item in geral:
                texto = str(item).strip()
                if texto:
                    return texto
        return "Sim. Todos os blocos possuem escadas."

    filtrados = [m for m in marcadores if m.get("tipo") == tipo]
    if blocos_pedidos:
        blocos_set = {bloco.strip().lower() for bloco in blocos_pedidos if str(bloco).strip()}
        filtrados = [m for m in filtrados if str(m.get("bloco") or "").strip().lower() in blocos_set]
        bloco_txt = ", ".join(bloco.upper() for bloco in blocos_pedidos)
        if filtrados:
            return f"Sim. Tem {ACESSIBILIDADE_TIPOS[tipo]['rotulo'].lower()} no {bloco_txt}."
        return f"Nao encontrei {ACESSIBILIDADE_TIPOS[tipo]['rotulo'].lower()} cadastrada no {bloco_txt}."

    if filtrados:
        blocos_encontrados = sorted({str(m.get("bloco") or "").strip().upper() for m in filtrados if str(m.get("bloco") or "").strip()})
        if blocos_encontrados:
            if len(blocos_encontrados) == 1:
                return f"Sim. Tem {rotulo_plural} no {blocos_encontrados[0]}."
            return f"Sim. Tem {rotulo_plural} nos blocos {', '.join(blocos_encontrados)}."
        return f"Sim. Tem {rotulo_plural} cadastrados na POLI."

    if tipo == "elevador":
        return "Nao encontrei elevador cadastrado nesse bloco."
    if tipo == "rampa":
        return "Ainda nao tenho rampa cadastrada nesse bloco."
    return None


def formatar_blocos_acessibilidade(blocos: list[str]) -> str:
    partes: list[str] = []
    for bloco in blocos:
        bloco_norm = str(bloco or "").strip()
        if not bloco_norm:
            continue
        if bloco_norm.lower() == "biblioteca":
            partes.append("na biblioteca")
        else:
            partes.append(f"no {bloco_norm.upper()}")

    if not partes:
        return ""
    if len(partes) == 1:
        return partes[0]
    return ", ".join(partes[:-1]) + " e " + partes[-1]


def encontrar_acessibilidade_por_pergunta(pergunta: str) -> str | None:
    texto_norm = normalizar_texto(pergunta)
    if not texto_norm:
        return None

    acess_cfg = POLIA_CONFIG.get("acessibilidade") or {}
    marcadores = carregar_acessibilidade()
    blocos_pedidos = extrair_blocos_acessibilidade_pergunta(texto_norm)
    tipo_pedido = extrair_tipo_acessibilidade_pergunta(texto_norm)
    pergunta_lista = bool(re.search(r"\b(onde|quais|quais\s+sao|quais\s+tem|onde\s+tem)\b", texto_norm))

    pergunta_direta = bool(re.search(r"\b(existe|tem|ha|há|possui|conta\s+com)\b", texto_norm))
    if tipo_pedido and (pergunta_direta or blocos_pedidos):
        resposta_direta = responder_acessibilidade_por_bloco(tipo_pedido, blocos_pedidos, marcadores, acess_cfg)
        if resposta_direta:
            return resposta_direta

    consulta_geral = bool(re.search(r"\b(acessibilidade|acessivel|pcd|cadeirante|mobilidade)\b", texto_norm))
    if consulta_geral and not any(tipo in texto_norm for tipo in ACESSIBILIDADE_TIPOS):
        geral_cfg = acess_cfg.get("geral") or []
        if isinstance(geral_cfg, list):
            gerais = [str(x).strip() for x in geral_cfg if str(x).strip()]
            if gerais:
                return " | ".join(gerais[:3])

    tipos_pedidos = [tipo for tipo in ACESSIBILIDADE_TIPOS if tipo in texto_norm]
    if not tipos_pedidos:
        return None

    respostas = []
    for tipo in tipos_pedidos:
        itens = _montar_falas_acessibilidade_config(tipo)
        filtrados = [m for m in marcadores if m.get("tipo") == tipo]
        if blocos_pedidos:
            filtrados = [m for m in filtrados if not m.get("bloco") or m.get("bloco") in blocos_pedidos]
        if pergunta_lista and not blocos_pedidos:
            blocos_encontrados = []
            for marcador in filtrados:
                bloco = str(marcador.get("bloco") or "").strip()
                if bloco:
                    blocos_encontrados.append(bloco)
            blocos_encontrados = list(dict.fromkeys(blocos_encontrados))
            if blocos_encontrados:
                blocos_txt = formatar_blocos_acessibilidade(blocos_encontrados)
                if tipo == "elevador":
                    rotulo_lista = "elevadores"
                elif tipo == "escada":
                    rotulo_lista = "escadas"
                else:
                    rotulo_lista = "rampas"
                respostas.append(f"Tem {rotulo_lista} {blocos_txt}.")
                continue
        for marcador in filtrados:
            bloco = str(marcador.get("bloco") or "").strip().upper()
            texto = str(marcador.get("texto") or "").strip()
            trecho = bloco or "campus"
            if texto:
                trecho = f"{trecho} - {texto}"
            itens.append(trecho)

        # Remove duplicadas preservando ordem para evitar repeticao nas falas.
        itens = list(dict.fromkeys([i for i in itens if i]))

        if not itens:
            respostas.append(f"Nao encontrei {ACESSIBILIDADE_TIPOS[tipo]['rotulo'].lower()} cadastrada")
            continue
        if tipo == "elevador":
            rotulo_plural = "elevadores"
        elif tipo == "escada":
            rotulo_plural = "escadas"
        else:
            rotulo_plural = "rampas"
        respostas.append(f"{ACESSIBILIDADE_TIPOS[tipo]['rotulo']} {rotulo_plural}: " + "; ".join(itens[:5]))

    if respostas:
        return " | ".join(respostas)

    fallback = str(acess_cfg.get("fallback") or "").strip()
    if fallback:
        return fallback
    return "Ainda nao mapeei rampas, escadas ou elevadores no campus."


def serializar_acessibilidade_html(marcadores: list[dict[str, Any]]) -> str:
    itens = []
    for marcador in marcadores:
        tipo = str(marcador.get("tipo") or "rampa").strip().lower()
        cfg = ACESSIBILIDADE_TIPOS.get(tipo, ACESSIBILIDADE_TIPOS["rampa"])
        itens.append(
            "<div class=\"acess-marker\" data-id=\"{id}\" data-tipo=\"{tipo}\" style=\"left:{x}%;top:{y}%;--marker-cor:{cor};--marker-fundo:{fundo};\">"
            "<div class=\"acess-pin\"><span>{icone}</span></div>"
            "</div>".format(
                id=marcador.get("id", ""),
                tipo=tipo,
                x=marcador.get("x", 0),
                y=marcador.get("y", 0),
                cor=cfg["cor"],
                fundo=cfg["fundo"],
                icone=cfg["icone"],
            )
        )
    return "".join(itens)


def garantir_arquivo_texto(caminho: str) -> None:
    if os.path.exists(caminho):
        return
    with open(caminho, "a", encoding="utf-8"):
        pass


def resolver_bloco_id(texto: str) -> str | None:
    texto_norm = normalizar_texto(texto)
    if not texto_norm:
        return None
    if texto_norm.startswith("bloco "):
        return texto_norm if texto_norm in locais_campus else None
    if len(texto_norm) == 1 and texto_norm.isalpha():
        return aliases_blocos.get(texto_norm)
    return None


def resolver_blocos_ids(blocos_raw: list[str]) -> list[str]:
    blocos: list[str] = []
    for item in blocos_raw or []:
        bloco_id = resolver_bloco_id(item)
        if bloco_id and bloco_id not in blocos:
            blocos.append(bloco_id)
    return blocos


def normalizar_blocos_evento(evento: dict[str, Any]) -> list[str]:
    blocos = evento.get("blocos")
    if isinstance(blocos, list) and blocos:
        return [str(b).strip() for b in blocos if str(b).strip()]
    bloco_legado = str(evento.get("bloco") or "").strip()
    if bloco_legado:
        return [bloco_legado]
    return []


def formatar_blocos_evento(evento: dict[str, Any]) -> str:
    blocos = normalizar_blocos_evento(evento)
    if "campus" in [b.lower() for b in blocos]:
        return "CAMPUS INTEIRO"
    return ", ".join([b.upper() for b in blocos]) if blocos else "LOCAL NAO INFORMADO"


def adicionar_evento(titulo: str, email: str, blocos_raw: list[str], sala: str, campus_inteiro: bool) -> tuple[bool, str]:
    titulo = (titulo or "").strip()
    email = (email or "").strip()
    sala = (sala or "").strip()
    if not titulo:
        return False, "Titulo e obrigatorio."
    if not email or not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
        return False, "Email valido e obrigatorio."

    blocos = [] if campus_inteiro else resolver_blocos_ids(blocos_raw)
    if not campus_inteiro and not blocos:
        return False, "Selecione pelo menos um bloco ou marque campus inteiro."

    eventos = carregar_eventos()
    eventos.append(
        {
            "id": uuid.uuid4().hex,
            "titulo": titulo,
            "email": email,
            "blocos": ["campus"] if campus_inteiro else blocos,
            "sala": sala,
        }
    )
    salvar_eventos(eventos)
    registrar_email_evento(email, titulo, blocos, sala, campus_inteiro)
    return True, "Evento salvo."


def remover_evento(evento_id: str) -> bool:
    if not evento_id:
        return False
    eventos = carregar_eventos()
    restantes = [e for e in eventos if str(e.get("id") or "") != evento_id]
    if len(restantes) == len(eventos):
        return False
    salvar_eventos(restantes)
    return True


def encontrar_evento_por_pergunta(pergunta: str) -> dict[str, Any] | None:
    eventos = carregar_eventos()
    if not eventos:
        return None
    pergunta_norm = normalizar_texto(pergunta)
    if not pergunta_norm:
        return None

    melhor: tuple[int, dict[str, Any]] | None = None
    for evento in eventos:
        titulo = str(evento.get("titulo") or "").strip()
        titulo_norm = normalizar_texto(titulo)
        if not titulo_norm:
            continue
        if titulo_norm in pergunta_norm:
            score = len(titulo_norm)
            if not melhor or score > melhor[0]:
                melhor = (score, evento)

    if melhor:
        return melhor[1]

    melhor_fuzzy: tuple[float, dict[str, Any]] | None = None
    for evento in eventos:
        titulo = str(evento.get("titulo") or "").strip()
        titulo_norm = normalizar_texto(titulo)
        if len(titulo_norm) < 4:
            continue
        score = SequenceMatcher(None, pergunta_norm, titulo_norm).ratio()
        if score >= 0.86 and (not melhor_fuzzy or score > melhor_fuzzy[0]):
            melhor_fuzzy = (score, evento)
    return melhor_fuzzy[1] if melhor_fuzzy else None


def _da_config_valida() -> bool:
    return bool(DA_USER and DA_PASSWORD)


def _da_assinatura(usuario: str) -> str:
    chave = DA_SECRET.encode("utf-8")
    payload = f"{usuario}:{DA_PASSWORD}".encode("utf-8")
    return hmac.new(chave, payload, hashlib.sha256).hexdigest()


def _da_gerar_token(usuario: str) -> str:
    return f"{usuario}:{_da_assinatura(usuario)}"


def _da_autenticado(request: Request) -> bool:
    token = request.cookies.get(DA_COOKIE_NAME)
    if not token:
        return False
    if ":" not in token:
        return False
    usuario, assinatura = token.split(":", 1)
    if not usuario or usuario != DA_USER:
        return False
    return hmac.compare_digest(assinatura, _da_assinatura(usuario))


@app.get("/", response_class=HTMLResponse)
async def ler_index() -> str:
    candidatos = [
        os.path.join(STATIC_DIR, "front"),
        os.path.join(STATIC_DIR, "index.html"),
        os.path.join(STATIC_DIR, "index"),
        "front",
        "index.html",
        "index",
    ]
    for caminho in candidatos:
        if os.path.exists(caminho):
            with open(caminho, "r", encoding="utf-8") as f:
                return f.read()
    return "<h1>Arquivo de interface nao encontrado.</h1>"


@app.get("/da/login", response_class=HTMLResponse)
async def pagina_login_da() -> str:
        if not _da_config_valida():
                return """
                <h1>Configuração do D.A ausente</h1>
                <p>Defina as variáveis de ambiente <strong>DA_USER</strong> e <strong>DA_PASSWORD</strong>.</p>
                """

        return """
        <!DOCTYPE html>
        <html lang="pt-br">
        <head>
            <meta charset="UTF-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            <title>Acesso D.A</title>
            <style>
                @import url('https://fonts.googleapis.com/css2?family=Baloo+2:wght@600;800&family=Space+Grotesk:wght@400;600;700&display=swap');
                :root {
                    --pink-strong: #c74788;
                    --pink-soft: #ffe1f1;
                    --rose: #8b1f4f;
                    --green-strong: #1f8b3b;
                    --green-bright: #7eea4b;
                }
                * { box-sizing: border-box; }
                body {
                    font-family: "Space Grotesk", "Nunito", sans-serif;
                    background: radial-gradient(circle at 20% 10%, #fff8fc 0, #ffe6f3 35%, #ffd4e8 100%);
                    margin: 0;
                    display: grid;
                    place-items: center;
                    min-height: 100vh;
                    color: #4a1634;
                    padding: 18px;
                }
                .backdrop {
                    position: fixed;
                    inset: 0;
                    pointer-events: none;
                    background:
                        radial-gradient(circle at 80% 20%, rgba(255, 255, 255, 0.8), rgba(255, 255, 255, 0) 40%),
                        radial-gradient(circle at 10% 80%, rgba(255, 241, 210, 0.7), rgba(255, 241, 210, 0) 42%);
                }
                .card {
                    position: relative;
                    background: #ffffff;
                    border-radius: 20px;
                    padding: 22px 22px 20px;
                    box-shadow: 0 22px 36px rgba(117, 25, 74, 0.18);
                    width: min(420px, 92vw);
                    border: 2px solid rgba(199, 71, 136, 0.45);
                    overflow: hidden;
                }
                .card::before {
                    content: "";
                    position: absolute;
                    inset: 0;
                    background: radial-gradient(circle at 90% 0%, rgba(255, 225, 241, 0.7), rgba(255, 225, 241, 0) 55%);
                    pointer-events: none;
                }
                h1 {
                    margin: 0 0 10px;
                    color: var(--rose);
                    font-family: "Baloo 2", sans-serif;
                    font-size: 1.6rem;
                    letter-spacing: 0.3px;
                }
                .subtitle {
                    margin: 0 0 14px;
                    font-size: 0.95rem;
                    color: #6d2850;
                }
                label {
                    display: block;
                    font-weight: 700;
                    color: #6d2850;
                    margin-top: 10px;
                }
                input {
                    width: 100%;
                    height: 42px;
                    border-radius: 12px;
                    border: 2px solid #e1a8c6;
                    padding: 0 12px;
                    margin-top: 6px;
                    font-size: 0.95rem;
                    outline: none;
                    transition: border-color 0.18s ease, box-shadow 0.18s ease;
                }
                input:focus {
                    border-color: #ffce6a;
                    box-shadow: 0 0 0 3px rgba(255, 206, 106, 0.3);
                }
                button {
                    margin-top: 16px;
                    width: 100%;
                    height: 46px;
                    border-radius: 14px;
                    border: 2px solid #17692c;
                    background: linear-gradient(180deg, var(--green-bright), var(--green-strong));
                    color: #0f3318;
                    font-family: "Baloo 2", sans-serif;
                    font-size: 1.05rem;
                    font-weight: 800;
                    cursor: pointer;
                    box-shadow: 0 12px 18px rgba(25, 95, 39, 0.25);
                    transition: transform 0.12s ease;
                }
                button:hover { transform: translateY(-1px); }
                .hint { margin-top: 12px; font-size: 0.86rem; color: #6d2850; }
            </style>
        </head>
        <body>
            <div class="backdrop" aria-hidden="true"></div>
            <form class="card" method="post" action="/da/login">
                <h1>Diretório Acadêmico</h1>
                <p class="subtitle">Acesso interno do D.A</p>
                <label for="usuario">Usuário</label>
                <input id="usuario" name="usuario" type="text" autocomplete="username" required />
                <label for="senha">Senha</label>
                <input id="senha" name="senha" type="password" autocomplete="current-password" required />
                <button type="submit">Entrar</button>
                <div class="hint">Acesso restrito ao D.A</div>
            </form>
        </body>
        </html>
        """


@app.post("/da/login")
async def autenticar_da(usuario: str = Form(...), senha: str = Form(...)):
        if not _da_config_valida():
                return HTMLResponse(
                        "<h1>Configuração do D.A ausente</h1>",
                        status_code=500,
                )

        if usuario != DA_USER or senha != DA_PASSWORD:
                return HTMLResponse(
                        "<h1>Credenciais inválidas</h1><p><a href=\"/da/login\">Voltar</a></p>",
                        status_code=401,
                )

        resposta = RedirectResponse(url="/da", status_code=302)
        resposta.set_cookie(
                DA_COOKIE_NAME,
                _da_gerar_token(usuario),
                httponly=True,
                samesite="lax",
                secure=False,
                max_age=60 * 60 * 8,
        )
        return resposta


@app.get("/da", response_class=HTMLResponse)
async def pagina_da(request: Request) -> HTMLResponse:
    if not _da_autenticado(request):
        return RedirectResponse(url="/da/login", status_code=302)

    eventos_html = ""
    blocos_html = ""
    if DA_EVENTOS_ATIVO:
        eventos = carregar_eventos()
        eventos_html = "".join(
            [
                f"<li><strong>{e.get('titulo','')}</strong> — {formatar_blocos_evento(e)}"
                f"{(' / ' + e.get('sala','')) if e.get('sala') else ''}"
                f" <form method=\"post\" action=\"/da/eventos/remover\" style=\"display:inline; margin-left: 8px;\">"
                f"<input type=\"hidden\" name=\"evento_id\" value=\"{e.get('id','')}\" />"
                f"<button type=\"submit\" style=\"padding: 4px 8px; border-radius: 10px; border: 1px solid #d98ab2;\">Remover</button>"
                f"</form></li>"
                for e in eventos
            ]
        ) or "<li>Nenhum evento cadastrado.</li>"

        blocos_html = "".join(
            [
                f"<label style=\"display:flex; align-items:center; gap:8px;\">"
                f"<input type=\"checkbox\" name=\"blocos\" value=\"{bid}\" />"
                f"{bid.upper()}</label>"
                for bid in sorted([b for b in locais_campus.keys() if b.startswith("bloco ")])
            ]
        )

    return HTMLResponse(
        """
        <!DOCTYPE html>
        <html lang="pt-br">
        <head>
            <meta charset="UTF-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            <title>D.A - Diretório Acadêmico</title>
            <style>
                body { font-family: "Nunito", "Segoe UI", sans-serif; margin: 0; background: #ffeaf6; color: #5b1b3f; }
                header { padding: 18px 20px; background: #c94887; color: #fff; display: flex; justify-content: space-between; align-items: center; }
                header h1 { margin: 0; font-family: "Baloo 2", sans-serif; font-size: 1.4rem; }
                header a { color: #fff; text-decoration: none; font-weight: 700; }
                header .actions { display: flex; gap: 12px; align-items: center; }
                main { padding: 18px 20px; }
                .card { background: #fff; border: 2px solid #d98ab2; border-radius: 16px; padding: 18px; box-shadow: 0 12px 20px rgba(117, 25, 74, 0.18); }
            </style>
        </head>
        <body>
            <header>
                <h1>Diretório Acadêmico</h1>
                <div class="actions">
                    <a href="/">Mapa</a>
                    <a href="/da/acessibilidade">Acessibilidade</a>
                    <a href="/da/cadastros/emails/download">Baixar emails</a>
                    <a href="/da/logout">Sair</a>
                </div>
            </header>
            <main>
                <div class="card">
                    <p>Conteúdo privado do D.A. Pode atualizar este bloco com comunicados e links internos.</p>
                </div>
                {eventos_panel}
            </main>
        </body>
        </html>
        """
        .replace(
            "{eventos_panel}",
            (
                """
                <div class=\"card\" style=\"margin-top: 18px;\">
                    <h2 style=\"margin: 0 0 10px;\">Eventos do campus</h2>
                    <p style=\"margin-top: 0;\">Cadastre eventos para a Polia informar onde acontecem. O email do responsavel e obrigatorio.</p>
                    <form method=\"post\" action=\"/da/eventos\" style=\"display: grid; gap: 12px;\">
                        <div>
                            <label for=\"titulo\" style=\"font-weight: 700;\">Titulo do evento</label>
                            <input id=\"titulo\" name=\"titulo\" type=\"text\" required />
                        </div>
                        <div>
                            <label for=\"email\" style=\"font-weight: 700;\">Email do responsavel</label>
                            <input id=\"email\" name=\"email\" type=\"email\" required placeholder=\"nome@exemplo.com\" />
                        </div>
                        <div>
                            <label style=\"font-weight: 700;\">Cobertura</label>
                            <label style=\"display:flex; align-items:center; gap:8px; margin-top: 6px;\">
                                <input type=\"checkbox\" name=\"campus_inteiro\" value=\"1\" />
                                Campus inteiro
                            </label>
                        </div>
                        <div>
                            <label style=\"font-weight: 700;\">Blocos (selecione um ou mais)</label>
                            <div style=\"display:grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 6px; margin-top: 6px;\">
                                {blocos_html}
                            </div>
                        </div>
                        <div>
                            <label for=\"sala\" style=\"font-weight: 700;\">Sala/area (opcional)</label>
                            <input id=\"sala\" name=\"sala\" type=\"text\" placeholder=\"Ex: Auditorio, Laboratorio 1\" />
                        </div>
                        <button type=\"submit\" style=\"margin-top: 6px;\">Salvar evento</button>
                    </form>
                    <h3 style=\"margin: 16px 0 8px;\">Eventos cadastrados</h3>
                    <ul style=\"margin: 0; padding-left: 18px;\">
                        {eventos_html}
                    </ul>
                    <p style=\"margin: 14px 0 0; font-size: 0.92rem; color: #7a3a5f;\">Os emails enviados ficam registrados em eventos_emails.txt.</p>
                </div>
                """.replace("{eventos_html}", eventos_html).replace("{blocos_html}", blocos_html)
                if DA_EVENTOS_ATIVO
                else ""
            ),
        )
    )


@app.get("/da/logout")
async def sair_da() -> RedirectResponse:
        resposta = RedirectResponse(url="/da/login", status_code=302)
        resposta.delete_cookie(DA_COOKIE_NAME)
        return resposta


@app.get("/da/cadastros/emails/download")
async def baixar_emails_cadastro(request: Request):
    if not _da_autenticado(request):
        return RedirectResponse(url="/da/login", status_code=302)

    garantir_arquivo_texto(CADASTRO_EMAILS_ARQUIVO)
    return FileResponse(
        CADASTRO_EMAILS_ARQUIVO,
        media_type="text/plain; charset=utf-8",
        filename="cadastro_emails.txt",
    )


@app.get("/da/acessibilidade", response_class=HTMLResponse)
async def pagina_acessibilidade_da(request: Request) -> HTMLResponse:
        if not _da_autenticado(request):
                return RedirectResponse(url="/da/login", status_code=302)

        blocos_options = "".join(
                [f'<option value="{bid}">{bid.upper()}</option>' for bid in sorted([b for b in locais_campus.keys() if b.startswith("bloco ")])]
        )
        marcadores = carregar_acessibilidade()
        marcadores_html = serializar_acessibilidade_html(marcadores)

        return HTMLResponse(
                f"""
                <!DOCTYPE html>
                <html lang="pt-br">
                <head>
                        <meta charset="UTF-8" />
                        <meta name="viewport" content="width=device-width, initial-scale=1" />
                        <title>D.A - Acessibilidade</title>
                        <style>
                                @import url('https://fonts.googleapis.com/css2?family=Baloo+2:wght@600;800&family=Nunito:wght@500;700;800&display=swap');
                                body {{ margin: 0; font-family: "Nunito", sans-serif; background: linear-gradient(180deg, #ffe7f2, #ffd1e6); color: #5b1b3f; }}
                                .shell {{ min-height: 100vh; padding: 18px; }}
                                .header {{ display:flex; justify-content:space-between; align-items:center; gap: 12px; margin-bottom: 14px; }}
                                .header h1 {{ margin:0; font-family:"Baloo 2", sans-serif; font-size: 1.6rem; }}
                                .header a {{ text-decoration:none; color:#7f1f4f; font-weight:800; background:#fff; border:2px solid #d98ab2; border-radius:12px; padding:8px 12px; }}
                                .grid {{ display:grid; grid-template-columns: 320px 1fr; gap: 14px; align-items:start; }}
                                .card {{ background:#fff; border:2px solid #d98ab2; border-radius:18px; padding:16px; box-shadow: 0 12px 20px rgba(117,25,74,.18); }}
                                .toolbar {{ display:grid; gap:10px; }}
                                label {{ display:block; font-weight:800; margin-bottom:6px; }}
                                input, select, button {{ font: inherit; }}
                                input, select {{ width:100%; min-height:42px; border-radius:12px; border:2px solid #e1a8c6; padding:0 12px; }}
                                button {{ min-height:42px; border-radius:12px; border:2px solid #16702c; background: linear-gradient(180deg, #95e95b, #279f39); font-weight:800; color:#103417; cursor:pointer; }}
                                .secondary {{ border-color:#d98ab2; background: linear-gradient(180deg, #fff6fb, #f4d8e6); color:#7f1f4f; }}
                                .map-wrap {{ position:relative; width:100%; aspect-ratio: 1.4 / 1; overflow:hidden; border-radius:18px; border:2px solid #d98ab2; background:#fff; }}
                                .map-wrap img {{ position:absolute; inset:0; width:100%; height:100%; object-fit:cover; }}
                                #checkpoints-editor-layer {{ position:absolute; inset:0; z-index:6; pointer-events:none; }}
                                #acessibilidade-editor-layer {{ position:absolute; inset:0; z-index:7; }}
                                .checkpoint-editor {{ position:absolute; width:42px; height:42px; transform:translate(-50%, -100%); border-radius:50% 50% 50% 0; rotate:-45deg; background: radial-gradient(circle at 35% 35%, #eaf8ff, #93d9ff 62%, #5dbcf2); border:2px solid #2f8dc0; box-shadow: 0 8px 18px rgba(0,0,0,.26); display:flex; align-items:center; justify-content:center; opacity:.88; }}
                                .checkpoint-editor span {{ transform: rotate(45deg); color:#0f4f70; font-family:"Baloo 2", sans-serif; font-size:.8rem; font-weight:800; margin-top:1px; }}
                                .acess-marker {{ position:absolute; transform:translate(-50%, -100%); display:flex; flex-direction:column; align-items:center; gap:4px; cursor:grab; user-select:none; touch-action:none; }}
                                .acess-marker:active {{ cursor:grabbing; }}
                                .acess-pin {{ width:38px; height:38px; border-radius: 50% 50% 50% 0; transform: rotate(-45deg); display:grid; place-items:center; font-size:16px; font-weight:900; background: var(--marker-fundo); color: var(--marker-cor); border:2px solid var(--marker-cor); box-shadow: 0 10px 18px rgba(0,0,0,.2); position:relative; }}
                                .acess-pin span {{ transform: rotate(45deg); display:inline-block; }}
                                .marker-list {{ display:grid; gap:8px; margin-top: 12px; }}
                                .marker-item {{ display:flex; justify-content:space-between; align-items:center; gap:8px; border:1px solid #efbfd5; border-radius:12px; padding:10px 12px; }}
                                .marker-actions {{ display:flex; gap:8px; }}
                                .marker-actions button {{ min-height:32px; padding:0 10px; }}
                                .hint {{ color:#7a3a5f; font-size:.92rem; line-height:1.35; }}
                                .editor-legend {{ margin-top: 12px; display:grid; gap:8px; }}
                                .editor-legend-item {{ display:flex; gap:10px; align-items:center; padding:8px 10px; border-radius:12px; background:#fff6fb; border:1px solid #efbfd5; }}
                                @media (max-width: 900px) {{ .grid {{ grid-template-columns: 1fr; }} }}
                        </style>
                </head>
                <body>
                        <div class="shell">
                                <div class="header">
                                        <h1>Editor de Acessibilidade</h1>
                                        <a href="/da">Voltar ao D.A</a>
                                </div>
                                <div class="grid">
                                        <div class="card">
                                                <div class="toolbar">
                                                        <div>
                                                                <label for="tipo-marker">Tipo</label>
                                                                <select id="tipo-marker">
                                                                        <option value="rampa">Rampa</option>
                                                                        <option value="escada">Escada</option>
                                                                        <option value="elevador">Elevador</option>
                                                                </select>
                                                        </div>
                                                        <div>
                                                                <label for="bloco-marker">Bloco</label>
                                                                <select id="bloco-marker">
                                                                        <option value="">Sem bloco</option>
                                                                        {blocos_options}
                                                                </select>
                                                        </div>
                                                        <div>
                                                                <label for="texto-marker">Observação</label>
                                                                <input id="texto-marker" type="text" placeholder="Ex: entrada lateral / corredor principal" />
                                                        </div>
                                                        <button id="btn-adicionar-marker" type="button">Adicionar marcador</button>
                                                        <button id="btn-salvar-marker" class="secondary" type="button">Salvar alterações</button>
                                                        <div class="hint">Arraste os ícones no mapa e depois salve. Cada marcador representa uma rampa, escada ou elevador.</div>
                                                </div>
                                                <div class="marker-list" id="lista-marcadores"></div>
                                                    <div class="editor-legend">
                                                        <div class="editor-legend-item"><div class="acess-pin" style="background:#daf8df;color:#1f8b3b;border-color:#1f8b3b;"><span>↗</span></div>Rampa</div>
                                                        <div class="editor-legend-item"><div class="acess-pin" style="background:#f7e3cb;color:#8b5a2b;border-color:#8b5a2b;"><span>🪜</span></div>Escada</div>
                                                        <div class="editor-legend-item"><div class="acess-pin" style="background:#d9e9ff;color:#1f5aa8;border-color:#1f5aa8;"><span>⇅</span></div>Elevador</div>
                                                    </div>
                                        </div>
                                        <div class="card">
                                                <div class="map-wrap" id="mapa-editor-wrap">
                                                        <img src="/static/mapa_poli_base.png" alt="Mapa do campus" />
                                                    <div id="checkpoints-editor-layer"></div>
                                                        <div id="acessibilidade-editor-layer">{marcadores_html}</div>
                                                </div>
                                        </div>
                                </div>
                        </div>
                        <script>
                        const locaisEditor = {{}};
                        const markersLayer = document.getElementById('acessibilidade-editor-layer');
                        const checkpointsEditorLayer = document.getElementById('checkpoints-editor-layer');
                        const listaMarcadores = document.getElementById('lista-marcadores');
                        const tipoMarker = document.getElementById('tipo-marker');
                        const blocoMarker = document.getElementById('bloco-marker');
                        const textoMarker = document.getElementById('texto-marker');
                        const btnAdicionarMarker = document.getElementById('btn-adicionar-marker');
                        const btnSalvarMarker = document.getElementById('btn-salvar-marker');
                        const MAPA_EDITOR = document.getElementById('mapa-editor-wrap');
                        let marcadores = {json.dumps(marcadores)};

                        const CORES = {{
                            rampa: {{ cor: '#1f8b3b', fundo: '#daf8df', icone: '↗' }},
                            escada: {{ cor: '#8b5a2b', fundo: '#f7e3cb', icone: '🪜' }},
                            elevador: {{ cor: '#1f5aa8', fundo: '#d9e9ff', icone: '⇅' }},
                        }};

                        function gerarId() {{
                            return (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2);
                        }}

                        function normalizarNumero(v, min, max) {{
                            return Math.max(min, Math.min(max, Number(v) || 0));
                        }}

                        function pontoInicialPorBloco(bloco) {{
                            const id = (bloco || '').trim().toLowerCase();
                            if (!id) return {{ x: 50, y: 50 }};
                            const ref = locaisEditor[id];
                            if (ref && typeof ref.x === 'number' && typeof ref.y === 'number') return {{ x: ref.x, y: ref.y }};
                            return {{ x: 50, y: 50 }};
                        }}

                        function tipoConfig(tipo) {{
                            return CORES[(tipo || 'rampa').toLowerCase()] || CORES.rampa;
                        }}

                        function abreviarLocalEditor(id) {{
                            const mapa = {{ "entrada": "ENT", "estacionamento": "EST", "lanchonete": "LAN", "bloco a": "A", "bloco h": "H", "bloco g": "G", "bloco f": "F", "bloco b": "B", "da": "DA", "bloco i/k": "I/K", "bloco e": "E", "bloco d": "D", "bloco j": "J", "bloco c": "C" }};
                            if (mapa[id]) return mapa[id];
                            if ((id || '').startsWith('bloco ')) return id.replace('bloco ', '').trim().toUpperCase() || 'BLO';
                            return String(id || '').slice(0, 3).toUpperCase();
                        }}

                        function renderizarCheckpointsEditor() {{
                            if (!checkpointsEditorLayer) return;
                            checkpointsEditorLayer.innerHTML = '';
                            Object.entries(locaisEditor).forEach(([id, dados]) => {{
                                if (!dados || typeof dados.x !== 'number' || typeof dados.y !== 'number') return;
                                const ck = document.createElement('div');
                                ck.className = 'checkpoint-editor';
                                ck.style.left = `${{dados.x}}%`;
                                ck.style.top = `${{dados.y}}%`;
                                ck.innerHTML = `<span>${{abreviarLocalEditor(id)}}</span>`;
                                checkpointsEditorLayer.appendChild(ck);
                            }});
                        }}

                        function renderizarLista() {{
                            listaMarcadores.innerHTML = '';
                            if (!marcadores.length) {{
                                listaMarcadores.innerHTML = '<div class="hint">Nenhum marcador ainda. Adicione rampas, escadas ou elevadores.</div>';
                                return;
                            }}
                            marcadores.forEach((item, index) => {{
                                const linha = document.createElement('div');
                                linha.className = 'marker-item';
                                linha.innerHTML = `<div><strong>${{(item.tipo || '').toUpperCase()}}</strong><br>${{(item.bloco || 'Sem bloco').toUpperCase()}}${{item.texto ? '<br><small>' + item.texto + '</small>' : ''}}</div>`;
                                const acoes = document.createElement('div');
                                acoes.className = 'marker-actions';
                                const btnIr = document.createElement('button');
                                btnIr.type = 'button';
                                btnIr.className = 'secondary';
                                btnIr.textContent = 'Ir';
                                btnIr.addEventListener('click', () => {{
                                    const el = markersLayer.querySelector(`[data-id="${{item.id}}"]`);
                                    if (el) el.scrollIntoView({{ block: 'center', behavior: 'smooth' }});
                                }});
                                const btnRemover = document.createElement('button');
                                btnRemover.type = 'button';
                                btnRemover.className = 'secondary';
                                btnRemover.textContent = 'Remover';
                                btnRemover.addEventListener('click', () => {{
                                    marcadores.splice(index, 1);
                                    renderizarEditor();
                                }});
                                acoes.appendChild(btnIr);
                                acoes.appendChild(btnRemover);
                                linha.appendChild(acoes);
                                listaMarcadores.appendChild(linha);
                            }});
                        }}

                        function aplicarPosicao(el, item) {{
                            el.style.left = `${{item.x}}%`;
                            el.style.top = `${{item.y}}%`;
                        }}

                        function renderizarEditor() {{
                            markersLayer.innerHTML = '';
                            marcadores.forEach((item) => {{
                                const cfg = tipoConfig(item.tipo);
                                const el = document.createElement('div');
                                el.className = 'acess-marker';
                                el.dataset.id = item.id;
                                el.dataset.tipo = item.tipo;
                                el.style.setProperty('--marker-cor', cfg.cor);
                                el.style.setProperty('--marker-fundo', cfg.fundo);
                                el.title = `${{cfg.rotulo}}${{item.bloco ? ' • ' + item.bloco.toUpperCase() : ''}}${{item.texto ? ' • ' + item.texto : ''}}`;
                                el.innerHTML = `<div class="acess-pin"><span>${{cfg.icone}}</span></div>`;
                                aplicarPosicao(el, item);

                                let drag = null;
                                el.addEventListener('pointerdown', (ev) => {{
                                    ev.preventDefault();
                                    el.setPointerCapture(ev.pointerId);
                                    const rect = MAPA_EDITOR.getBoundingClientRect();
                                    drag = {{ x: item.x, y: item.y, offsetX: ev.clientX - rect.left, offsetY: ev.clientY - rect.top }};
                                    el.style.zIndex = '20';
                                }});
                                el.addEventListener('pointermove', (ev) => {{
                                    if (!drag) return;
                                    const rect = MAPA_EDITOR.getBoundingClientRect();
                                    const x = normalizarNumero(((ev.clientX - rect.left) / rect.width) * 100, 0, 100);
                                    const y = normalizarNumero(((ev.clientY - rect.top) / rect.height) * 100, 0, 100);
                                    item.x = x;
                                    item.y = y;
                                    aplicarPosicao(el, item);
                                }});
                                const finalizar = () => {{
                                    if (!drag) return;
                                    drag = null;
                                    el.style.zIndex = '7';
                                }};
                                el.addEventListener('pointerup', finalizar);
                                el.addEventListener('pointercancel', finalizar);
                                el.addEventListener('dblclick', () => {{
                                    marcadores = marcadores.filter((m) => m.id !== item.id);
                                    renderizarEditor();
                                }});
                                markersLayer.appendChild(el);
                            }});
                            renderizarLista();
                        }}

                        function adicionarMarcador() {{
                            const tipo = (tipoMarker.value || 'rampa').toLowerCase();
                            const bloco = (blocoMarker.value || '').trim().toLowerCase();
                            const pos = pontoInicialPorBloco(bloco);
                            marcadores.push({{
                                id: gerarId(),
                                tipo,
                                x: pos.x,
                                y: pos.y,
                                bloco,
                                texto: (textoMarker.value || '').trim(),
                                rotulo: tipoConfig(tipo).rotulo,
                            }});
                            textoMarker.value = '';
                            renderizarEditor();
                        }}

                        async function salvarMarcadores() {{
                            const resposta = await fetch('/api/acessibilidade', {{
                                method: 'POST',
                                headers: {{ 'Content-Type': 'application/json' }},
                                body: JSON.stringify({{ marcadores }}),
                            }});
                            const data = await resposta.json().catch(() => ({{}}));
                            if (!resposta.ok || data.status === 'erro') {{
                                alert('Nao foi possivel salvar agora.');
                                return;
                            }}
                            alert('Marcadores salvos.');
                        }}

                        btnAdicionarMarker.addEventListener('click', adicionarMarcador);
                        btnSalvarMarker.addEventListener('click', salvarMarcadores);
                        (async () => {{
                            try {{
                                const res = await fetch('/api/locais');
                                const data = await res.json();
                                Object.assign(locaisEditor, data || {{}});
                                renderizarCheckpointsEditor();
                            }} catch (_) {{}}
                            renderizarEditor();
                        }})();
                        </script>
                </body>
                </html>
                """
        )


@app.post("/da/eventos")
async def salvar_evento_da(
    request: Request,
    titulo: str = Form(...),
    email: str = Form(...),
    blocos: list[str] = Form([]),
    sala: str = Form(""),
    campus_inteiro: str = Form(""),
):
    if not _da_autenticado(request):
        return RedirectResponse(url="/da/login", status_code=302)

    if not DA_EVENTOS_ATIVO:
        return HTMLResponse(
            "<h1>Eventos desativados</h1><p>Esta funcionalidade esta desligada.</p><p><a href=\"/da\">Voltar</a></p>",
            status_code=404,
        )

    ok, msg = adicionar_evento(titulo, email, blocos, sala, bool(campus_inteiro))
    if not ok:
        return HTMLResponse(
            f"<h1>Erro ao salvar evento</h1><p>{msg}</p><p><a href=\"/da\">Voltar</a></p>",
            status_code=400,
        )
    return RedirectResponse(url="/da", status_code=302)


@app.post("/da/eventos/remover")
async def remover_evento_da(request: Request, evento_id: str = Form("")):
    if not _da_autenticado(request):
        return RedirectResponse(url="/da/login", status_code=302)

    if not DA_EVENTOS_ATIVO:
        return HTMLResponse(
            "<h1>Eventos desativados</h1><p>Esta funcionalidade esta desligada.</p><p><a href=\"/da\">Voltar</a></p>",
            status_code=404,
        )

    if not remover_evento(evento_id):
        return HTMLResponse(
            "<h1>Evento nao encontrado</h1><p><a href=\"/da\">Voltar</a></p>",
            status_code=404,
        )
    return RedirectResponse(url="/da", status_code=302)


@app.get("/api/locais")
async def listar_locais() -> dict[str, Any]:
    return locais_campus


@app.get("/api/rotas")
async def listar_rotas() -> dict[str, Any]:
    return {
        "locais_override": POLIA_CONFIG.get("locais_override") or {},
        "rotas_offset": POLIA_CONFIG.get("rotas_offset") or {},
    }


@app.post("/api/rotas")
async def salvar_rotas(payload: RotasPayload) -> dict[str, Any]:
    config_atual = carregar_config_polia()
    if payload.locais_override is not None:
        config_atual["locais_override"] = payload.locais_override
    if payload.rotas_offset is not None:
        config_atual["rotas_offset"] = payload.rotas_offset

    salvar_config_polia(config_atual)
    global POLIA_CONFIG
    POLIA_CONFIG = config_atual
    aplicar_locais_override(payload.locais_override)
    return {"status": "ok"}


@app.get("/api/acessibilidade")
async def listar_acessibilidade_api() -> list[dict[str, Any]]:
    return carregar_acessibilidade()


@app.post("/api/acessibilidade")
async def salvar_acessibilidade_api(request: Request, payload: AcessibilidadePayload) -> dict[str, Any]:
    if not _da_autenticado(request):
        return {"status": "erro", "mensagem": "Nao autenticado"}

    marcadores = []
    for item in payload.marcadores:
        marcador = normalizar_marcador_acessibilidade(item.model_dump())
        if marcador:
            marcadores.append(marcador)

    salvar_acessibilidade(marcadores)
    return {"status": "ok", "total": len(marcadores)}


@app.post("/api/guiar")
async def guiar_usuario(req: RequisicaoLocal) -> dict[str, Any]:
    destino_id = req.destino.lower().strip()
    if destino_id in locais_campus:
        dados = locais_campus[destino_id]
        texto_avatar = await gerar_fala_com_ia(destino_id, dados, contexto_extra=req.contexto_extra)
        return {
            "status": "sucesso",
            "coordenadas": {"x": dados["x"], "y": dados["y"]},
            "salas": dados["salas"],
            "dica": dados["dica"],
            "texto": texto_avatar,
            "animacao": metadados_animacao_para_texto(texto_avatar),
        }
    return {"status": "erro", "mensagem": "Local não encontrado."}


@app.post("/api/chat")
async def chat_veterano(req: RequisicaoChat) -> dict[str, Any]:
    rota = inferir_rota_por_texto(req.pergunta)
    if rota:
        origem = rota.get("origem")
        destino = rota.get("destino")
        if origem and destino:
            msg = f"Fechou! Vou te guiar de {origem.upper()} ate {destino.upper()}."
            return {
                "status": "sucesso",
                "tipo": "rota",
                "origem": origem,
                "destino": destino,
                "texto": msg,
                "animacao": metadados_animacao_para_texto(msg),
            }
        if origem:
            msg = f"Perfeito! Agora considero que voce esta em {origem.upper()}."
            return {
                "status": "sucesso",
                "tipo": "origem",
                "origem": origem,
                "texto": msg,
                "animacao": metadados_animacao_para_texto(msg),
            }

    pergunta_norm = normalizar_texto(req.pergunta)
    lanche_info = _detectar_consulta_lanche(pergunta_norm)
    if lanche_info["tem_lanche"]:
        if lanche_info["tem_magaiver"]:
            texto = "Os Lanches do Magaiver ficam na Portaria, logo na entrada principal."
        elif lanche_info["tem_bloco_a"]:
            texto = "A lanchonete da POLI fica atras do Bloco A, aquele predio rosa."
        else:
            texto = "Tem dois pontos: Lanches do Magaiver na entrada/portaria e a lanchonete da POLI atras do Bloco A. Qual voce quer?"
        return {
            "status": "sucesso",
            "tipo": "lanche",
            "texto": texto,
            "animacao": metadados_animacao_para_texto(texto),
        }

    acessibilidade = encontrar_acessibilidade_por_pergunta(req.pergunta)
    if acessibilidade:
        return {
            "status": "sucesso",
            "tipo": "acessibilidade",
            "texto": acessibilidade,
            "animacao": metadados_animacao_para_texto(acessibilidade),
        }

    busca = buscar_destino_por_sala(req.pergunta)
    if busca:
        destino = busca["destino"]
        sala = busca["sala"]
        texto = f"Achei {sala}. {descrever_localizacao_destino(destino)}"
        return {
            "status": "sucesso",
            "tipo": "sala",
            "destino": destino,
            "texto": texto,
            "animacao": metadados_animacao_para_texto(texto),
        }

    evento = encontrar_evento_por_pergunta(req.pergunta) if DA_EVENTOS_ATIVO else None
    if evento:
        titulo = str(evento.get("titulo") or "").strip()
        blocos = normalizar_blocos_evento(evento)
        sala = str(evento.get("sala") or "").strip()
        campus_inteiro = "campus" in [b.lower() for b in blocos]
        if campus_inteiro:
            texto = f"O evento {titulo} acontece no campus inteiro."
        elif sala:
            texto = f"O evento {titulo} acontece no {', '.join([b.upper() for b in blocos])}, sala {sala}."
        else:
            texto = f"O evento {titulo} acontece no {', '.join([b.upper() for b in blocos])}."
        return {
            "status": "sucesso",
            "tipo": "evento",
            "destino": blocos[0] if blocos else "",
            "texto": texto,
            "animacao": metadados_animacao_para_texto(texto),
        }

    inferencia_ia = await inferir_destino_com_ia(req.pergunta, contexto_extra=req.contexto_extra)
    if inferencia_ia:
        destino = inferencia_ia["destino"]
        texto = f"Entendi tua busca. {descrever_localizacao_destino(destino)}"
        return {
            "status": "sucesso",
            "tipo": "sala",
            "destino": destino,
            "texto": texto,
            "animacao": metadados_animacao_para_texto(texto),
        }

    chat_cfg = POLIA_CONFIG.get("chat") or {}
    objetivo_chat = chat_cfg.get("objetivo") or "Responder duvida geral do calouro de forma util e natural."
    regras_chat = list(
        chat_cfg.get("regras") or ["Responda com no maximo 3 frases.", "Seja direto e natural."]
    )
    regras_chat.append(
        "Se a pergunta estiver fora do contexto da POLI U-P-E, diga que voce so fala sobre o campus e sugira perguntar por bloco ou sala."
    )

    prompt = montar_prompt_polia(
        objetivo=objetivo_chat,
        dados={"pergunta": req.pergunta},
        regras=regras_chat,
        contexto_extra=req.contexto_extra,
    )

    if not genai_client and not OPENAI_API_KEY:
        texto = chat_cfg.get("fallback") or "Tenta me perguntar por bloco ou sala, tipo B01, K06 ou Biblioteca, que eu te guio rapidinho."
        return {
            "status": "sucesso",
            "texto": texto,
            "animacao": metadados_animacao_para_texto(texto),
        }

    try:
        if OPENAI_API_KEY:
            texto = (gerar_texto_openai(prompt, temperatura=0.7, max_tokens=180) or "").strip()
        else:
            response = genai_client.models.generate_content(model=GEMINI_TEXT_MODEL, contents=prompt)
            texto = (response.text or "").strip()
        if not texto:
            texto = obter_resposta_fora_contexto()
        return {"status": "sucesso", "texto": texto, "animacao": metadados_animacao_para_texto(texto)}
    except Exception as e:
        print(f"Erro no chat IA: {e}")
        texto = obter_resposta_fora_contexto()
        return {
            "status": "sucesso",
            "texto": texto,
            "animacao": metadados_animacao_para_texto(texto),
        }


@app.get("/api/avatar-config")
async def avatar_config() -> dict[str, Any]:
    frames = listar_frames_avatar()
    frames_apresentacao = listar_frames_apresentacao()
    fim_apresentacao = max(0, len(frames_apresentacao) - 1)
    return {
        "static_dir": STATIC_DIR,
        "frames": {
            "pasta": localizar_pasta_frames_avatar(),
            "total": len(frames),
            "arquivos": frames,
        },
        "apresentacao": {
            "inicio": 0,
            "fim": fim_apresentacao,
            "arquivos": frames_apresentacao,
            "abertura": {
                "inicio": 0,
                "fim": fim_apresentacao,
            },
            "loop": {
                "inicio": 0,
                "fim": fim_apresentacao,
            },
        },
        "boca": {
            "intervalo_min_ms": 45,
            "intervalo_max_ms": 220,
            "max_frames_sequencia": 160,
        },
    }


class CadastroEmailPayload(BaseModel):
    email: str


@app.post("/api/cadastro-email")
async def registrar_cadastro_email(payload: CadastroEmailPayload) -> dict[str, str]:
    email = (payload.email or "").strip()
    if not email or not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
        return {"status": "erro", "mensagem": "Email invalido"}

    with open(CADASTRO_EMAILS_ARQUIVO, "a", encoding="utf-8") as f:
        f.write(email + "\n")

    return {"status": "ok"}


def gerar_audio_openai(texto: str) -> bytes | None:
    if not OPENAI_API_KEY:
        return None

    texto_limpo = (texto or "").strip()
    if not texto_limpo:
        return None

    payload = {
        "model": OPENAI_TTS_MODEL,
        "voice": OPENAI_TTS_VOICE,
        "input": texto_limpo,
        "response_format": "mp3",
        "instructions": OPENAI_TTS_INSTRUCTIONS,
    }

    request = urllib.request.Request(
        url="https://api.openai.com/v1/audio/speech",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENAI_API_KEY}",
        },
    )

    try:
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(request, timeout=30, context=ssl_ctx) as response:
            audio = response.read()
        return audio or None
    except urllib.error.HTTPError as e:
        erro = e.read().decode("utf-8", errors="ignore")
        print(f"Erro OpenAI TTS HTTP {e.code}: {erro}")
        return None
    except Exception as e:
        print(f"Erro OpenAI TTS: {e}")
        return None


@app.post("/api/tts")
async def gerar_tts(req: RequisicaoTTS):
    audio = gerar_audio_openai(req.texto)
    if not audio:
        return {"status": "erro", "mensagem": "TTS indisponível"}
    return Response(content=audio, media_type="audio/mpeg")


if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "9000"))
    uvicorn.run(app, host=host, port=port, reload=False)
