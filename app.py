import chainlit as cl
import os
from pydantic import create_model, Field
import logging
import httpx
from contextlib import AsyncExitStack
import asyncio
import base64
import json

# Imports oficiais e estáveis do MCP [cite: 1]
from mcp import ClientSession
from anyio import create_memory_object_stream

# Imports do LangChain e LangGraph [cite: 1]
from langchain_openai import ChatOpenAI
from langchain_core.tools import StructuredTool
from langgraph.prebuilt import create_react_agent
from langchain_core.messages import HumanMessage

# =====================================================================
# CONFIGURAÇÕES (LIDAS EXCLUSIVAMENTE DO .ENV)
# =====================================================================
# Variáveis e Credenciais da LLM
LLM_TOKEN_URL = os.getenv("LLM_TOKEN_URL")
LLM_CLIENT_ID = os.getenv("LLM_CLIENT_ID")
LLM_CLIENT_SECRET = os.getenv("LLM_CLIENT_SECRET")
GATEWAY_LLM_URL = os.getenv("GATEWAY_LLM_URL")
MODELO_LLM = os.getenv("MODELO_LLM", "gpt-5-nano")

# Variáveis e Credenciais do MCP 
MCP_TOKEN_URL = os.getenv("MCP_TOKEN_URL")
MCP_CLIENT_ID = os.getenv("MCP_CLIENT_ID")
MCP_CLIENT_SECRET = os.getenv("MCP_CLIENT_SECRET")
GATEWAY_MCP_URL = os.getenv("GATEWAY_MCP_URL")

# Validação preventiva de infraestrutura
if not all([LLM_TOKEN_URL, LLM_CLIENT_ID, LLM_CLIENT_SECRET, GATEWAY_LLM_URL,
            MCP_TOKEN_URL, MCP_CLIENT_ID, MCP_CLIENT_SECRET, GATEWAY_MCP_URL]):
    raise ValueError("❌ Erro crítico: Faltam variáveis essenciais no arquivo .env para a Sensedia!")


# Filtro de log do MCP para limpar o terminal
class IgnorarKeepaliveMCP(logging.Filter):
    def filter(self, record):
        mensagem = record.getMessage()
        if "Failed to validate notification" in mensagem and "$/keepalive" in mensagem:
            return False
        return True

logging.getLogger().addFilter(IgnorarKeepaliveMCP())


# =====================================================================
# FUNÇÃO DE AUTENTICAÇÃO PADRÃO KEYCLOAK (VIA CORPO DA REQUISIÇÃO)
# =====================================================================
async def obter_token_keycloak(token_url: str, client_id: str, client_secret: str) -> str:
    """Gera um token OAuth enviando obrigatoriamente as credenciais no corpo (Keycloak)"""
    payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    async with httpx.AsyncClient() as client:
        resposta = await client.post(token_url, data=payload, headers=headers)
        
        if resposta.status_code != 200:
            print(f"[ERRO KEYCLOAK] URL: {token_url} | Código: {resposta.status_code} | Detalhes: {resposta.text}")
            resposta.raise_for_status()
            
        dados = resposta.json()
        return dados["access_token"]


# =====================================================================
# MOTOR DE INICIALIZAÇÃO DO AGENTE DO SISTEMA (VALIDADO)
# =====================================================================
async def inicializar_agente_sistema():
    """Inicializa os clientes coletando ferramentas do MCP e autenticando a LLM via Keycloak"""
    exit_stack = AsyncExitStack()
    
    # -----------------------------------------------------------------
    # PARTE 1: AUTENTICAÇÃO KEYCLOAK E MAPEAMENTO DO MCP
    # -----------------------------------------------------------------
    print("[MCP] Solicitando token de acesso ao Keycloak...")
    token_mcp = await obter_token_keycloak(MCP_TOKEN_URL, MCP_CLIENT_ID, MCP_CLIENT_SECRET)

    # Injetamos os aceites combinados que desarmam o erro 406 Not Acceptable
    headers_mcp = {
        "Authorization": f"Bearer {token_mcp.strip()}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    
    meu_cliente_http = httpx.AsyncClient(headers=headers_mcp, timeout=30.0)
    await exit_stack.enter_async_context(meu_cliente_http)

    print("[MCP] Executando Handshake inicial...")
    payload_handshake = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "id": 0,
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "Chainlit OpenFinance Bot", "version": "1.0.0"}
        }
    }

    # Passo 1: Handshake inicial na URL raiz
    resposta_init = await meu_cliente_http.post(GATEWAY_MCP_URL, json=payload_handshake)
    if resposta_init.status_code != 200:
        raise RuntimeError(f"Falha no handshake inicial com a Sensedia: {resposta_init.text}")

    print("[MCP] Handshake aceito! Rastreando Header de Sessão para evitar Erro 422...")

    # Captura dinâmica do ID de sessão nos metadados para amarrar os requests seguintes
    id_sessao_detectado = None
    for chave, valor in resposta_init.headers.items():
        chave_minuscula = chave.lower()
        if "session" in chave_minuscula or "mcp" in chave_minuscula:
            if "id" in chave_minuscula or "session" in chave_minuscula:
                id_sessao_detectado = valor

    if not id_sessao_detectado:
        # Se omitido pelo proxy, tratamos o payload para ler do corpo limpo
        texto_init = resposta_init.text.strip()
        if texto_init.startswith("data:"):
            texto_init = texto_init[5:].strip()
        try:
            dados_init = json.loads(texto_init)
            id_sessao_detectado = dados_init.get("sessionId") or dados_init.get("result", {}).get("sessionId")
        except:
            pass

    if not id_sessao_detectado:
        id_sessao_detectado = "0"

    print(f"[MCP] Sessão vinculada com sucesso: {id_sessao_detectado}")

    # Injetamos o ID capturado em todas as variações para o Gateway não retornar 'session header is required'
    headers_mcp["X-MCP-Session-ID"] = id_sessao_detectado
    headers_mcp["X-Session-ID"] = id_sessao_detectado
    headers_mcp["Mcp-Session-Id"] = id_sessao_detectado
    headers_mcp["Session-Id"] = id_sessao_detectado

    # Payload oficial para listar ferramentas
    payload_tools = {
        "jsonrpc": "2.0",
        "method": "tools/list",
        "id": 1,
        "params": {}
    }

    # Passo 2: Listagem de ferramentas com os Headers de sessão devidamente injetados
    resposta_tools = await meu_cliente_http.post(GATEWAY_MCP_URL, json=payload_tools, headers=headers_mcp)
    
    # Se der 404 na raiz, tenta o redirecionamento automático para o canal de mensagens
    if resposta_tools.status_code == 404:
        URL_MENSAGENS_MCP = f"{GATEWAY_MCP_URL.rstrip('/')}/messages"
        resposta_tools = await meu_cliente_http.post(URL_MENSAGENS_MCP, json=payload_tools, headers=headers_mcp)

    if resposta_tools.status_code != 200:
        raise RuntimeError(f"Falha ao listar ferramentas no Gateway: {resposta_tools.text}")

    texto_tools = resposta_tools.text.strip()
    if texto_tools.startswith("data:"):
        texto_tools = texto_tools[5:].strip()

    dados_tools = json.loads(texto_tools)
    resultado_mcp = dados_tools.get("result", {})
    
    if isinstance(resultado_mcp, list) and len(resultado_mcp) > 0:
        lista_ferramentas_puras = resultado_mcp[0].get("tools", [])
    else:
        lista_ferramentas_puras = resultado_mcp.get("tools", [])

    print(f"[MCP] Mapeadas {len(lista_ferramentas_puras)} ferramentas Open Finance com sucesso!")

    # Executor interno síncrono/sequencial adaptado ao barramento Sensedia
    def criar_executor_ferramenta(nome_ferramenta):
        async def executar_ferramenta_sensedia(**kwargs):
            payload_call = {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 99,
                "params": {"name": nome_ferramenta, "arguments": kwargs}
            }
            try:
                res = await meu_cliente_http.post(GATEWAY_MCP_URL, json=payload_call, headers=headers_mcp)
                if res.status_code == 404:
                    URL_MENSAGENS_MCP = f"{GATEWAY_MCP_URL.rstrip('/')}/messages"
                    res = await meu_cliente_http.post(URL_MENSAGENS_MCP, json=payload_call, headers=headers_mcp)
                
                if res.status_code == 200:
                    txt = res.text.strip()
                    if txt.startswith("data:"):
                        txt = txt[5:].strip()
                    obj = json.loads(txt)
                    content_list = obj.get("result", {}).get("content", [])
                    return "\n".join([c.get("text", "") for c in content_list if "text" in c])
                return f"Erro na API da Sensedia: Status {res.status_code}"
            except Exception as e:
                return f"Falha ao executar ferramenta Open Finance: {str(e)}"
        return executar_ferramenta_sensedia

    # Conversão de Schema dinâmico para o LangChain
    ferramentas_langchain = []
    for t in lista_ferramentas_puras:
        nome = t.get("name")
        descricao = t.get("description", "")
        schema_input = t.get("inputSchema", {})
        propriedades = schema_input.get("properties", {})
        obrigatorios = schema_input.get("required", [])
        
        campos_pydantic = {}
        for nome_param, detalhes in propriedades.items():
            tipo_json = detalhes.get("type", "string")
            tipo_py = str
            if tipo_json == "integer": tipo_py = int
            elif tipo_json == "number": tipo_py = float
            elif tipo_json == "boolean": tipo_py = bool
            descricao_param = detalhes.get("description", f"Parâmetro {nome_param}")
            if nome_param in obrigatorios:
                campos_pydantic[nome_param] = (tipo_py, Field(..., description=descricao_param))
            else:
                campos_pydantic[nome_param] = (tipo_py, Field(default=None, description=descricao_param))

        EsquemaDinamico = create_model(f"{nome}Schema", **campos_pydantic)
        ferramentas_langchain.append(
            StructuredTool.from_function(
                name=nome,
                description=descricao,
                coroutine=criar_executor_ferramenta(nome),
                args_schema=EsquemaDinamico
            )
        )

    # -----------------------------------------------------------------
    # PARTE 2: AUTENTICAÇÃO KEYCLOAK E INSTANCIAÇÃO DA LLM
    # -----------------------------------------------------------------
    print("[LLM] Solicitando token de acesso ao Keycloak...")
    token_llm = await obter_token_keycloak(LLM_TOKEN_URL, LLM_CLIENT_ID, LLM_CLIENT_SECRET)
    
    token_llm_limpo = token_llm.strip()
    url_base_llm = GATEWAY_LLM_URL.strip().rstrip('/')

    print(f"[LLM] Token Keycloak gerado. Vinculando cabeçalho Authorization Bearer...")

    # Headers limpos e explícitos exigidos pelo Proxy de IA para Completions sem dar 401
    headers_llm_sensedia = {
        "Authorization": f"Bearer {token_llm_limpo}",
        "Content-Type": "application/json"
    }

    llm = ChatOpenAI(
        model=MODELO_LLM, 
        base_url=url_base_llm,
        api_key=token_llm_limpo, 
        default_headers=headers_llm_sensedia
    )
    
    # Acopla a LLM e as ferramentas estruturadas do Open Finance no Agente ReAct
    agente = create_react_agent(llm, tools=ferramentas_langchain)
    
    # Salva as sessões ativas no contexto global do Chainlit
    cl.user_session.set("exit_stack", exit_stack)
    cl.user_session.set("agente_langgraph", agente)
    print("[SISTEMA] Inicialização concluída com sucesso! Ambos os barramentos estão conectados.")


# =====================================================================
# EVENTOS DO CHAINLIT
# =====================================================================
@cl.on_chat_start
async def on_chat_start():
    cl.user_session.set("historico_mensagens", [])
    
    msg_status = cl.Message(content="⚙️ *Estabelecendo conexão segura com o AI Gateway Sensedia...*")
    await msg_status.send()

    try:
        await inicializar_agente_sistema()
        
        texto_boas_vindas = """
# 🏦 Assistente Open Finance Corporativo Conectado!

Olá! Eu sou o seu agente de inteligência artificial integrado a dados no padrão **Open Finance** via Sensedia AI Gateway. 

Como posso ajudar você hoje?

### 🚀 Exemplos do que você pode me pedir:
* *"Qual o saldo consolidado das contas da carteira?"*
* *"Consulte os contratos de crédito vigentes e o valor das parcelas."*
* *"Quais propostas de financiamento estão disponíveis no momento?"*
        """
        msg_status.content = texto_boas_vindas
        await msg_status.update()

    except Exception as e:
        msg_status.content = f"❌ **Falha na inicialização do sistema:** Não foi possível autenticar ou conectar aos endpoints da Sensedia.\n\n*Detalhes do erro: {str(e)}*"
        await msg_status.update()


@cl.on_message
async def main(message: cl.Message):
    agente = cl.user_session.get("agente_langgraph")
    
    if not agente:
        await cl.Message(content="❌ O sistema não está conectado adequadamente. Verifique os logs e as chaves do seu arquivo `.env`.").send()
        return

    msg_chainlit = cl.Message(content="")
    await msg_chainlit.send()

    historico = cl.user_session.get("historico_mensagens", [])
    historico.append(HumanMessage(content=message.content))
    config = {"configurable": {"thread_id": cl.user_session.get("id")}}

    try:
        async for event in agente.astream_events({"messages": historico}, config, version="v2"):
            kind = event["event"]
            if kind == "on_chat_model_stream":
                chunk = event["data"]["chunk"].content
                if chunk:
                    await msg_chainlit.stream_token(chunk)
        
        historico.append(msg_chainlit.content)
        cl.user_session.set("historico_mensagens", historico)
        await msg_chainlit.update()

    except Exception as e:
        await cl.Message(content=f"❌ **Erro inesperado durante o processamento:** {str(e)}").send()


@cl.on_chat_end
async def on_chat_end():
    exit_stack = cl.user_session.get("exit_stack")
    if exit_stack:
        try:
            await exit_stack.aclose()
        except:
            pass