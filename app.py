import chainlit as cl
import os
from pydantic import create_model, Field
import logging
import httpx
from contextlib import AsyncExitStack
import asyncio

# Imports do MCP
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

# Imports do LangChain e LangGraph
from langchain_openai import ChatOpenAI
from langchain_core.tools import StructuredTool
from langgraph.prebuilt import create_react_agent
from langchain_core.messages import HumanMessage

# =====================================================================
# CONFIGURAÇÕES (AI GATEWAY E MCP)
# =====================================================================
CUMBUCA_MCP_URL = os.environ.get("CUMBUCA_MCP_URL", "https://mcp.cumbuca.com/mcp")

# Configurações do seu AI Gateway (Substitua pelos seus dados reais)
GATEWAY_TOKEN_URL = os.environ.get("GATEWAY_TOKEN_URL", "https://api-solutions-garage.sensedia.com/dev/ai/oauth/v1/access-token")
GATEWAY_CLIENT_ID = os.environ.get("GATEWAY_CLIENT_ID", "3d6f28cf-91fa-43fa-9382-86005229574b")
GATEWAY_CLIENT_SECRET = os.environ.get("GATEWAY_CLIENT_SECRET", "7e08865c-4810-4384-9605-ba5e9d01351d")
GATEWAY_BASE_URL = os.environ.get("GATEWAY_BASE_URL", "https://solutions-garage-ai-gateway-lab.sensedia-eng.com/personal-finance-agent/") # URL base compatível com OpenAI
MODELO_LLM = os.environ.get("MODELO_LLM", "gpt-5-nano")

# Filtro de log do MCP (Mantido para limpar o terminal)
class IgnorarKeepaliveMCP(logging.Filter):
    def filter(self, record):
        mensagem = record.getMessage()
        if "Failed to validate notification" in mensagem and "$/keepalive" in mensagem:
            return False
        return True

logging.getLogger().addFilter(IgnorarKeepaliveMCP())


# =====================================================================
# FUNÇÃO DE AUTENTICAÇÃO DO AI GATEWAY (ATUALIZADA)
# =====================================================================
async def obter_token_ai_gateway() -> str:
    """Busca o token OAuth dinâmico usando Basic Auth, padrão da Sensedia."""
    async with httpx.AsyncClient() as client:
        # Quando usamos Basic Auth, o payload só precisa do grant_type
        payload = {
            "grant_type": "client_credentials"
        }
        
        # O parâmetro 'auth' pega o ID e o Secret, converte para Base64 e
        # injeta automaticamente no cabeçalho: "Authorization: Basic xxxxx"
        resposta = await client.post(
            GATEWAY_TOKEN_URL, 
            data=payload,
            auth=(GATEWAY_CLIENT_ID, GATEWAY_CLIENT_SECRET)
        )
        
        # Se ainda der erro, isso vai imprimir o motivo exato no terminal
        if resposta.status_code != 200:
            print(f"[ERRO SENSEDIA] Detalhes da recusa: {resposta.text}")
            resposta.raise_for_status()
            
        dados = resposta.json()
        return dados["access_token"]


# =====================================================================
# NOVA EXCEÇÃO PARA CAPTURAR O TOKEN VENCIDO NO MEIO DO CHAT
# =====================================================================
class TokenExpiradoError(Exception):
    pass

# =====================================================================
# A PONTE: MCP -> LANGCHAIN
# =====================================================================
def converter_ferramenta_mcp_para_langchain(mcp_tool, session: ClientSession):
    async def executar_ferramenta(**kwargs):
        # Limpa qualquer aviso antigo antes de rodar
        cl.user_session.set("token_vencido", False)
        
        try:
            # MÁGICA 1: Colocamos um limite de 10 segundos. Se o background morrer, isso não congela mais!
            resultado = await asyncio.wait_for(
                session.call_tool(mcp_tool.name, arguments=kwargs), 
                timeout=10.0
            )
            return "\n".join([c.text for c in resultado.content if hasattr(c, 'text')])
            
        except asyncio.TimeoutError:
            # Se demorou e a bandeira de 401 foi levantada no background, explode o erro certo!
            if cl.user_session.get("token_vencido"):
                raise TokenExpiradoError("Token expirado no background.")
            return f"Erro: A Cumbuca demorou mais de 10s para responder (Timeout)."
            
        except Exception as e:
            if "401" in str(e) or cl.user_session.get("token_vencido"):
                raise TokenExpiradoError("Token expirado.")
            return f"Erro na API de acesso aos dados Open Finance: {str(e)}"

    # Seu código do Schema Dinâmico (Pydantic) continua aqui
    propriedades = mcp_tool.inputSchema.get("properties", {})
    obrigatorios = mcp_tool.inputSchema.get("required", [])
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

    EsquemaDinamico = create_model(f"{mcp_tool.name}Schema", **campos_pydantic)

    return StructuredTool.from_function(
        name=mcp_tool.name,
        description=mcp_tool.description,
        coroutine=executar_ferramenta,
        args_schema=EsquemaDinamico
    )

# =====================================================================
# MOTOR DE CONEXÃO REUTILIZÁVEL (Usado no Início e no Retry)
# =====================================================================
async def criar_agente_com_mcp(token_usuario):
    # 1. Limpeza Segura (Fecha conexões velhas se existirem)
    old_stack = cl.user_session.get("exit_stack")
    if old_stack:
        import asyncio
        try:
            await asyncio.sleep(0.5)
            await old_stack.aclose()
        except:
            pass

    exit_stack = AsyncExitStack()
    token_gateway = await obter_token_ai_gateway()

   # MÁGICA 2: O espião da rede que levanta a bandeira
    async def disparar_erro_se_falhar(response):
        if response.status_code == 401:
            cl.user_session.set("token_vencido", True)
        response.raise_for_status()

    headers_mcp = {
        "Authorization": f"Bearer {token_usuario}",
        "Content-Type": "application/json"
    }
    
    meu_cliente_http = httpx.AsyncClient(
        headers=headers_mcp, 
        timeout=30.0, 
        event_hooks={'response': [disparar_erro_se_falhar]}
    )
    
    await exit_stack.enter_async_context(meu_cliente_http)
    streams = await exit_stack.enter_async_context(
        streamable_http_client(CUMBUCA_MCP_URL, http_client=meu_cliente_http)
    )
    session = await exit_stack.enter_async_context(ClientSession(streams[0], streams[1]))
    await session.initialize()

    lista_mcp = await session.list_tools()
    ferramentas_langchain = [converter_ferramenta_mcp_para_langchain(t, session) for t in lista_mcp.tools]

    llm = ChatOpenAI(
        model=MODELO_LLM, 
        base_url=GATEWAY_BASE_URL,
        api_key=token_gateway
    )
    agente = create_react_agent(llm, tools=ferramentas_langchain)
    
    # Salva tudo de novo na sessão
    cl.user_session.set("exit_stack", exit_stack)
    cl.user_session.set("mcp_session", session)
    cl.user_session.set("agente_langgraph", agente)
    return len(ferramentas_langchain)

# =====================================================================
# EVENTOS DO CHAINLIT
# =====================================================================
@cl.on_chat_start
async def on_chat_start():
    cl.user_session.set("historico_mensagens", [])
    
    # --- NOVO TEXTO DE BOAS-VINDAS ATUALIZADO ---
    texto_boas_vindas = """
# 🏦 Bem-vindo ao Assistente Open Finance

Olá! Eu sou o seu agente de inteligência artificial integrado ao **Open Finance**. 
Estou aqui para ajudar você a consultar informações financeiras e gerenciar operações de crédito de forma rápida, conversacional e segura.

### 🚀 O que eu posso fazer por você?
* **Consultas Rápidas:** Verificar saldos, históricos de pagamentos e status de contas vinculadas.
* **Gestão de Crédito:** Analisar propostas, limites disponíveis, contratos vigentes e parcelas em aberto.
* **Serviços Financeiros:** Auxiliar com diretrizes para renegociação, revisão de limites e portabilidade.

### 🔒 Segurança e Privacidade
A proteção dos seus dados é a nossa prioridade:
* O acesso requer **autenticação via Token e consentimento gerado no seu próprio banco** (suas credenciais não são salvas no banco de dados do chat).
* O contexto da conversa é **estritamente limitado** ao CPF/Conta consultados na sessão atual.
* As transações passam por camadas de segurança do nosso *Sensedia AI Gateway*.
    """
    
    # Envia a mensagem para a tela
    await cl.Message(content=texto_boas_vindas).send()
    # --------------------------------------------
    
    while True:
        resposta = await cl.AskUserMessage(
            content="🔑 **Conexão com o diretório Open**\n\nPor favor, cole seu Token de Autorização ao diretório Open Finance:", timeout=300
        ).send()

        if not resposta: return

        token = resposta['output'].strip()
        msg = cl.Message(content="🚀 Conectando...")
        await msg.send()

        try:
            qtd = await criar_agente_com_mcp(token)
            msg.content = f"✅ **Conectado com sua conta através do Open Finance!** Como posso ajudar?"
            await msg.update()
            break # Sucesso!
        except Exception as e:
            if "401" in str(e) or "Unauthorized" in str(e):
                msg.content = "❌ **Token inválido (401).** O servidor recusou a conexão. Tente novamente."
            else:
                msg.content = f"❌ **Erro na conexão:** {str(e)}"
            await msg.update()


@cl.on_message
async def main(message: cl.Message):
    # Guardamos o texto exato que o usuário enviou para podermos repetir a pergunta depois
    conteudo_mensagem_usuario = message.content 

    while True:
        agente = cl.user_session.get("agente_langgraph")
        if not agente: return

        msg_chainlit = cl.Message(content="")
        await msg_chainlit.send()

        historico = cl.user_session.get("historico_mensagens", [])
        # Adiciona a pergunta ao histórico antes de mandar pro LangGraph
        historico.append(HumanMessage(content=conteudo_mensagem_usuario))
        config = {"configurable": {"thread_id": cl.user_session.get("id")}}

        try:
            async for event in agente.astream_events({"messages": historico}, config, version="v2"):
                kind = event["event"]
                if kind == "on_chat_model_stream":
                    chunk = event["data"]["chunk"].content
                    if chunk:
                        await msg_chainlit.stream_token(chunk)
                # O LangGraph e o Chainlit vão rodar as ferramentas silenciosamente 
                # mantendo apenas a animação de "digitando..." padrão!
                #elif kind == "on_tool_start":
                #    await cl.Message(content=f"⚙️ *Consultando: `{event['name']}`...*").send()
            
            # Se a execução terminou sem erros, finalizamos
            historico.append(msg_chainlit.content)
            cl.user_session.set("historico_mensagens", historico)
            await msg_chainlit.update()
            break # Fim do ciclo normal!

        except TokenExpiradoError:
            # A MÁGICA 2: O token caiu durante a execução da ferramenta!
            await msg_chainlit.remove() # Apaga a resposta quebrada da tela
            historico.pop() # Remove a pergunta do histórico (para não ficar duplicada quando repetirmos)
            cl.user_session.set("historico_mensagens", historico)

            # Pausa tudo e pede um token novo pro usuário
            resposta = await cl.AskUserMessage(
                content="⚠️ **Seu Token expirou no meio da consulta!**\nPor favor, cole um novo token válido para eu refazer a busca automaticamente:", 
                timeout=300
            ).send()

            if not resposta: return

            novo_token = resposta['output'].strip()
            msg_retry = cl.Message(content="🔄 Reconectando...")
            await msg_retry.send()

            try:
                # Recria as conexões e o agente com o token novo
                await criar_agente_com_mcp(novo_token)
                await msg_retry.remove()
                
                # Ao dar continue, o 'while True' recomeça e a pergunta do usuário é processada de novo!
                continue 
            except Exception as e:
                await cl.Message(content=f"❌ Erro ao tentar reconectar: {e}").send()
                break

        except Exception as e:
            await cl.Message(content=f"❌ **Erro no processamento:** {str(e)}").send()
            break