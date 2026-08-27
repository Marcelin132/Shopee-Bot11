"""
Bot Shopee -> Telegram + Supabase (versão otimizada)
----------------------------------------------------
- Busca geral/relevância da Shopee.
- Filtra: >= 1000 vendas e >= 4.5 estrelas.
- Processa página por página: não espera terminar toda a busca.
- Posta assim que encontra um produto válido e ainda não enviado.
- Usa Supabase como histórico permanente.
- Mantém servidor HTTP para Render/UptimeRobot.
"""

import os
import sys
import json
import time
import html
import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

# ===================== CONFIGURAÇÕES =====================

SHOPEE_APP_ID = os.getenv("SHOPEE_APP_ID")
SHOPEE_SECRET = os.getenv("SHOPEE_SECRET")
SHOPEE_AFFILIATE_ID = os.getenv("SHOPEE_AFFILIATE_ID")
SHOPEE_API_URL = os.getenv(
    "SHOPEE_API_URL",
    "https://open-api.affiliate.shopee.com.br/graphql"
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
TELEGRAM_CHANNEL_NAME = os.getenv("TELEGRAM_CHANNEL_NAME", "")
TELEGRAM_CHANNEL_LINK = os.getenv("TELEGRAM_CHANNEL_LINK", "")

SHOPEE_SEARCH_KEYWORD = os.getenv("SHOPEE_SEARCH_KEYWORD", "")
SHOPEE_PRODUCT_LIMIT = int(os.getenv("SHOPEE_PRODUCT_LIMIT", "5"))
POST_INTERVAL_SEGUNDOS = int(os.getenv("POST_INTERVAL_SEGUNDOS", "30"))

SHOPEE_VENDAS_MINIMAS = int(os.getenv("SHOPEE_VENDAS_MINIMAS", "1000"))
SHOPEE_AVALIACAO_MINIMA = float(os.getenv("SHOPEE_AVALIACAO_MINIMA", "4.5"))

# Quantos produtos pedir por página.
# 50 é um bom equilíbrio entre velocidade e carga na API.
SHOPEE_BUSCA_BRUTA = int(os.getenv("SHOPEE_BUSCA_BRUTA", "50"))

# Intervalo entre requisições de páginas.
# Não deixe muito baixo para evitar rate limit da API.
SHOPEE_INTERVALO_PAGINAS = float(
    os.getenv("SHOPEE_INTERVALO_PAGINAS", "0.3")
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client | None = None

# Cache local em memória dos produtos que já foram enviados.
# O histórico permanente continua no Supabase.
postados_cache = set()


# ===================== CONFIGURAÇÃO =====================

def checar_configuracao():
    obrigatorias = {
        "SHOPEE_APP_ID": SHOPEE_APP_ID,
        "SHOPEE_SECRET": SHOPEE_SECRET,
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHANNEL_ID": TELEGRAM_CHANNEL_ID,
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_KEY": SUPABASE_KEY,
    }

    faltando = [k for k, v in obrigatorias.items() if not v]

    if faltando:
        print("Faltam configurar estas variáveis:")
        for nome in faltando:
            print(f"  - {nome}")
        sys.exit(1)

    global supabase
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    print("Supabase conectado com sucesso.")


# ===================== SUPABASE =====================

def id_do_produto(produto: dict) -> str:
    """ID estável. Primeiro usa itemId da Shopee."""
    if produto.get("itemId") is not None:
        return str(produto["itemId"])

    return str(
        produto.get("offerLink")
        or produto.get("productName", "")
    ).strip()


def carregar_historico_supabase():
    """
    Carrega os IDs já enviados para a memória.
    Faz paginação para não depender de um limite pequeno de linhas.
    """
    global postados_cache

    if not supabase:
        raise RuntimeError("Supabase não foi inicializado.")

    inicio = 0
    tamanho = 1000
    total = 0

    while True:
        resposta = (
            supabase
            .table("produtos_postados")
            .select("produto_id")
            .range(inicio, inicio + tamanho - 1)
            .execute()
        )

        linhas = resposta.data or []

        for linha in linhas:
            produto_id = linha.get("produto_id")
            if produto_id:
                postados_cache.add(str(produto_id))

        total += len(linhas)

        if len(linhas) < tamanho:
            break

        inicio += tamanho

    print(f"Histórico carregado do Supabase: {total} produtos.")


def salvar_produto_postado(produto: dict):
    """Salva depois que o Telegram confirmou o envio."""
    if not supabase:
        raise RuntimeError("Supabase não foi inicializado.")

    produto_id = id_do_produto(produto)
    link = produto.get("offerLink") or ""

    try:
        supabase.table("produtos_postados").insert({
            "produto_id": produto_id,
            "link": link,
        }).execute()

        postados_cache.add(produto_id)
        print(f"Salvo no Supabase: {produto_id}")

    except Exception as e:
        texto = str(e).lower()

        # UNIQUE evita duplicação mesmo em caso de corrida/reexecução.
        if (
            "duplicate" in texto
            or "unique" in texto
            or "23505" in texto
        ):
            postados_cache.add(produto_id)
            print(f"Produto já estava salvo no Supabase: {produto_id}")
        else:
            raise


# ===================== SHOPEE =====================

def gerar_assinatura(payload: str):
    timestamp = int(time.time())
    base_string = f"{SHOPEE_APP_ID}{timestamp}{payload}{SHOPEE_SECRET}"
    assinatura = hashlib.sha256(
        base_string.encode("utf-8")
    ).hexdigest()

    return assinatura, timestamp


QUERY_PRODUTOS = """
query productOfferV2($keyword: String, $page: Int, $limit: Int, $sortType: Int) {
  productOfferV2(
    keyword: $keyword,
    page: $page,
    limit: $limit,
    sortType: $sortType
  ) {
    nodes {
      itemId
      productName
      priceMin
      priceMax
      priceDiscountRate
      sales
      ratingStar
      offerLink
      imageUrl
    }
    pageInfo {
      page
      limit
      hasNextPage
    }
  }
}
"""


def buscar_pagina(pagina: int):
    """Busca UMA página. Assim podemos filtrar/postar antes da próxima."""
    limite = min(max(SHOPEE_BUSCA_BRUTA, 1), 500)

    variables = {
        "keyword": SHOPEE_SEARCH_KEYWORD or None,
        "page": pagina,
        "limit": limite,
        "sortType": 1,  # relevância / busca geral
    }

    body = {
        "query": QUERY_PRODUTOS,
        "variables": variables,
    }

    payload = json.dumps(
        body,
        separators=(",", ":")
    )

    assinatura, timestamp = gerar_assinatura(payload)

    headers = {
        "Content-Type": "application/json",
        "Authorization": (
            f"SHA256 Credential={SHOPEE_APP_ID}, "
            f"Timestamp={timestamp}, "
            f"Signature={assinatura}"
        ),
    }

    resposta = requests.post(
        SHOPEE_API_URL,
        headers=headers,
        data=payload,
        timeout=30,
    )

    resposta.raise_for_status()
    dados = resposta.json()

    if dados.get("errors"):
        raise Exception(
            f"Erro retornado pela API da Shopee: {dados['errors']}"
        )

    resultado = dados["data"]["productOfferV2"]

    return (
        resultado.get("nodes") or [],
        resultado.get("pageInfo") or {},
    )


def produto_passou_filtro(produto: dict) -> bool:
    try:
        vendas = float(produto.get("sales") or 0)
        avaliacao = float(produto.get("ratingStar") or 0)
    except (TypeError, ValueError):
        return False

    return (
        vendas >= SHOPEE_VENDAS_MINIMAS
        and avaliacao >= SHOPEE_AVALIACAO_MINIMA
    )


# ===================== TELEGRAM =====================

def _para_float(valor, padrao=0.0):
    try:
        return float(valor)
    except (TypeError, ValueError):
        return padrao


def formatar_valor_brl(valor: float) -> str:
    return f"R$ {valor:.2f}".replace(".", ",")


def calcular_precos(produto: dict):
    """
    Retorna (preco_atual, preco_original, percentual_desconto).

    priceMin/priceMax já vêm com o desconto aplicado.
    priceDiscountRate é a taxa de desconto (%) fornecida pela própria Shopee.
    O preço "original" é calculado a partir disso: original = atual / (1 - taxa/100)
    """
    preco_atual = _para_float(
        produto.get("priceMin") or produto.get("priceMax") or 0
    )

    taxa_desconto = _para_float(produto.get("priceDiscountRate"))

    preco_original = None
    percentual = 0

    if 0 < taxa_desconto < 100:
        preco_original = preco_atual / (1 - taxa_desconto / 100)
        percentual = round(taxa_desconto)

    return preco_atual, preco_original, percentual


def formatar_preco(produto: dict) -> str:
    preco_atual, _, _ = calcular_precos(produto)
    return formatar_valor_brl(preco_atual)


def formatar_bloco_preco(produto: dict) -> str:
    """
    Monta o bloco de preço para a mensagem do Telegram (HTML).
    Se houver desconto real informado pela Shopee, mostra o preço
    original riscado, o selo de % OFF e o preço final (já com o desconto).
    """
    preco_atual, preco_original, percentual = calcular_precos(produto)
    preco_atual_fmt = formatar_valor_brl(preco_atual)

    if preco_original and percentual > 0:
        preco_original_fmt = formatar_valor_brl(preco_original)
        return (
            f"<s>{preco_original_fmt}</s> 🏷️ -{percentual}% OFF\n"
            f"💵 <b>{preco_atual_fmt}</b>"
        )

    return f"💵 {preco_atual_fmt}"


def formatar_mensagem(produto: dict) -> str:
    nome = html.escape(produto.get("productName", "Produto"))
    bloco_preco = formatar_bloco_preco(produto)
    link = html.escape(produto.get("offerLink", ""), quote=False)
    nome_canal = html.escape(TELEGRAM_CHANNEL_NAME)
    link_canal = html.escape(TELEGRAM_CHANNEL_LINK, quote=False)

    return (
        f"🔥 {nome}\n\n"
        f"{bloco_preco}\n\n"
        f"🔗 {link}\n\n"
        f"{nome_canal}\n"
        f"{link_canal}\n\n"
        f"#Anuncio #SilvaPromos"
    )

def enviar_telegram(mensagem: str, imagem_url: str = None):
    if imagem_url:
        url = (
            f"https://api.telegram.org/"
            f"bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        )

        payload = {
            "chat_id": TELEGRAM_CHANNEL_ID,
            "photo": imagem_url,
            "caption": mensagem,
            "parse_mode": "HTML",
        }
    else:
        url = (
            f"https://api.telegram.org/"
            f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        )

        payload = {
            "chat_id": TELEGRAM_CHANNEL_ID,
            "text": mensagem,
            "parse_mode": "HTML",
        }

    resposta = requests.post(
        url,
        data=payload,
        timeout=30,
    )

    resposta.raise_for_status()
    return resposta.json()


# ===================== RENDER / UPTIMEROBOT =====================

class HealthHandler(BaseHTTPRequestHandler):

    def _responder_ok(self, corpo=True):
        body = b"ShopeeBot OK"

        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )
        self.send_header(
            "Content-Length",
            str(len(body))
        )
        self.end_headers()

        if corpo:
            self.wfile.write(body)

    def do_GET(self):
        self._responder_ok(True)

    def do_HEAD(self):
        self._responder_ok(False)

    def log_message(self, format, *args):
        return


def iniciar_servidor_http():
    porta = int(os.getenv("PORT", "10000"))

    servidor = ThreadingHTTPServer(
        ("0.0.0.0", porta),
        HealthHandler,
    )

    thread = threading.Thread(
        target=servidor.serve_forever,
        daemon=True,
    )

    thread.start()

    print(
        f"Servidor HTTP ativo na porta {porta} "
        f"(health check: /)"
    )

    return servidor


# ===================== PROCESSAMENTO OTIMIZADO =====================

def processar_produto(produto: dict) -> bool:
    """
    Tenta postar UM produto.
    Retorna True se postou com sucesso.
    """
    produto_id = id_do_produto(produto)

    # Primeiro verifica o cache em memória.
    if produto_id in postados_cache:
        return False

    if not produto_passou_filtro(produto):
        return False

    nome = produto.get("productName", "Produto")

    try:
        enviar_telegram(
            formatar_mensagem(produto),
            produto.get("imageUrl"),
        )

        # Só registra depois do Telegram confirmar o envio.
        salvar_produto_postado(produto)

        print(
            f"✅ Postado: {nome} | "
            f"vendas={produto.get('sales')} | "
            f"avaliação={produto.get('ratingStar')}"
        )

        return True

    except Exception as e:
        print(f"❌ Erro ao postar '{nome}': {e}")
        return False


def rodar_uma_vez():
    """
    Otimização principal:

    Antes:
      TODAS as páginas -> filtro -> Supabase -> posts.

    Agora:
      página -> filtro -> posta imediatamente -> próxima página.

    Para cada rodada, para ao atingir SHOPEE_PRODUCT_LIMIT.
    """
    print(
        "Buscando produtos da Shopee "
        "(geral/relevância, página por página)..."
    )

    limite_posts = max(SHOPEE_PRODUCT_LIMIT, 1)
    postados_nesta_rodada = 0
    pagina = 1

    while True:
        try:
            produtos, page_info = buscar_pagina(pagina)
        except Exception as e:
            print(f"Erro ao buscar página {pagina}: {e}")
            return

        print(
            f"Página {pagina}: {len(produtos)} produtos encontrados"
        )

        if not produtos:
            print("A Shopee não retornou mais produtos.")
            break

        validos = 0
        novos = 0

        for produto in produtos:
            if not produto_passou_filtro(produto):
                continue

            validos += 1
            produto_id = id_do_produto(produto)

            if produto_id in postados_cache:
                continue

            novos += 1

            # POSTA IMEDIATAMENTE.
            if processar_produto(produto):
                postados_nesta_rodada += 1

                if postados_nesta_rodada >= limite_posts:
                    print(
                        f"Limite da rodada atingido: "
                        f"{postados_nesta_rodada} produto(s)."
                    )
                    return

                # Pequena pausa entre posts para não floodar Telegram.
                time.sleep(2)

        print(
            f"Página {pagina}: {validos} passaram no filtro, "
            f"{novos} eram novos."
        )

        if not page_info.get("hasNextPage"):
            print("Fim das páginas disponíveis nesta busca.")
            break

        pagina += 1

        # Pequena pausa para respeitar a API.
        time.sleep(SHOPEE_INTERVALO_PAGINAS)

    if postados_nesta_rodada == 0:
        print("Nenhum produto novo encontrado nesta rodada.")


def rodar_continuamente():
    while True:
        inicio = time.time()

        try:
            rodar_uma_vez()
        except Exception as e:
            print(f"Erro no ciclo de postagem: {e}")

        duracao = time.time() - inicio

        print(
            f"Rodada terminada em {duracao:.1f}s. "
            f"Aguardando {POST_INTERVAL_SEGUNDOS}s..."
        )

        time.sleep(max(POST_INTERVAL_SEGUNDOS, 1))


# ===================== MAIN =====================

if __name__ == "__main__":
    checar_configuracao()
    carregar_historico_supabase()
    iniciar_servidor_http()

    if "--loop" in sys.argv:
        rodar_continuamente()
    else:
        rodar_uma_vez()
