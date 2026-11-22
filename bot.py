import html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    FUSO_BRASILIA = ZoneInfo("America/Sao_Paulo")
except ZoneInfoNotFoundError:
    FUSO_BRASILIA = timezone(timedelta(hours=-3))

import requests
from dotenv import load_dotenv
from playwright.sync_api import Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

# Configuração de logs
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("bot_passagem")

# URLs padrão para o trecho Brasília - DF -> Santa Rita de Cássia - BA em 18/12/2026
DEFAULT_URL_GUANABARA = (
    "https://viajeguanabara.com.br/onibus/brasilia-df-todos/santa_rita_de_cassia-ba/"
    "?departure_date=2026-12-18&passengers=13:1"
)
DEFAULT_URL_CLICKBUS = (
    "https://www.clickbus.com.br/onibus/brasilia-df-todos/santa-rita-cassia-ba"
    "?departureDate=2026-12-18&gratuity=true"
)

PADRAO_PRECO = re.compile(r"R\$\s*(\d{1,3}(?:\.\d{3})*)\s*,\s*(\d{2})")
PADRAO_TIPO_PASSAGEM = re.compile(
    r"\b(leito\s+individual|leito\s+cama|semi[-\s]?leito|executivo|convencional|leito|cama)\b",
    re.IGNORECASE,
)
PADRAO_HORARIO = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")
PADRAO_EMPRESA = re.compile(r"r[aá]pido\s*federal|guanabara", re.IGNORECASE)
PADRAO_SEM_RESULTADOS = re.compile(
    r"nenhuma\s+viagem|nenhum\s+resultado\s+encontrado|"
    r"n[aã]o\s+encontramos\s+(?:viagens|passagens|resultados|hor[aá]rios)|"
    r"sem\s+(?:viagens|passagens|hor[aá]rios|disponibilidade)|"
    r"n[aã]o\s+h[aá]\s+(?:viagens|passagens|hor[aá]rios)",
    re.IGNORECASE,
)

SELETORES_BOTAO_COMPRA_GUANABARA = [
    "button[data-testid='result-card-choose-seat']",
    "button:has-text('Escolher assento')",
    "button:has-text('Selecionar assento')",
    "button:has-text('Selecionar passagem')",
    "button:has-text('Ver poltronas')",
]

SELETORES_BOTAO_COMPRA_CLICKBUS = [
    "button[aria-label='Selecionar passagem']",
    "button[aria-label*='Selecionar passagem' i]",
    "button:has-text('Selecionar passagem')",
    "button:has-text('Escolher assento')",
    "button:has-text('Ver poltronas')",
    "[data-testid*='select-trip' i]",
]


def extrair_precos(texto: str, minimo: float = 1.0) -> list[str]:
    """Extrai valores monetários em reais (R$ XX,XX), inclusive quebrados em múltiplas linhas."""
    unicos: list[str] = []
    vistos: set[str] = set()
    for inteiro, centavos in PADRAO_PRECO.findall(texto):
        valor = float(f"{inteiro.replace('.', '')}.{centavos}")
        if valor < minimo:
            continue
        normalizado = f"R$ {inteiro},{centavos}"
        if normalizado not in vistos:
            vistos.add(normalizado)
            unicos.append(normalizado)
    return unicos


def criar_contexto_navegador(browser: Browser) -> BrowserContext:
    """Cria um contexto de navegador configurado para sites React/Next.js no Brasil."""
    context = browser.new_context(
        viewport={"width": 1366, "height": 900},
        locale="pt-BR",
        timezone_id="America/Sao_Paulo",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        extra_http_headers={
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    )
    return context


def _aguardar_carregamento_dinamico(
    page: Page,
    seletores_compra: list[str],
    timeout_segundos: int = 25,
) -> None:
    """Aguarda a renderização dinâmica (Next.js/React) até aparecer botão de compra ou aviso de indisponibilidade."""
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        logger.debug("networkidle expirou; seguindo com verificação ativa do DOM.")

    inicio = time.monotonic()
    seletor_combinado = ", ".join(seletores_compra)

    while time.monotonic() - inicio < timeout_segundos:
        try:
            if page.locator(seletor_combinado).first.is_visible():
                page.wait_for_timeout(1500)
                return
        except Exception:
            pass

        try:
            texto = page.inner_text("body")
            if PADRAO_PRECO.search(texto) or PADRAO_SEM_RESULTADOS.search(texto):
                page.wait_for_timeout(1500)
                return
        except Exception:
            pass

        page.wait_for_timeout(1000)


def _obter_texto_principal(page: Page) -> str:
    """Retorna o texto do elemento <main> (ou <body> se <main> não existir)."""
    try:
        main = page.locator("main").first
        if main.count() > 0:
            return main.inner_text()
    except Exception:
        pass
    return page.inner_text("body")


def _coletar_botoes_visiveis(page: Page, seletores: list[str]) -> list[str]:
    """Retorna os botões de compra visíveis na página (sem contar o mesmo elemento duas vezes)."""
    seletor_combinado = ", ".join(seletores)
    botoes_encontrados: list[str] = []
    locator = page.locator(seletor_combinado)

    try:
        total = locator.count()
    except Exception:
        return botoes_encontrados

    for idx in range(min(total, 50)):
        elemento = locator.nth(idx)
        try:
            if not elemento.is_visible():
                continue
            texto = re.sub(r"\s+", " ", (elemento.inner_text() or "").strip())
            aria = (elemento.get_attribute("aria-label") or "").strip()
            testid = (elemento.get_attribute("data-testid") or "").strip()
            rotulo = texto or aria or testid or "botão de compra"
            botoes_encontrados.append(rotulo)
        except Exception:
            continue

    return botoes_encontrados


def _detectar_mencao_empresa(page: Page, texto_principal: str) -> list[str]:
    """Detecta menções à Rápido Federal / Guanabara no conteúdo principal e em atributos alt/aria-label."""
    mencoes: set[str] = set()

    for match in PADRAO_EMPRESA.finditer(texto_principal):
        mencoes.add(re.sub(r"\s+", " ", match.group(0)).strip())

    try:
        atributos = page.evaluate(
            """() => {
                const raiz = document.querySelector('main') || document.body;
                const valores = [];
                raiz.querySelectorAll('img[alt], [aria-label], [title]').forEach(el => {
                    const alt = el.getAttribute('alt');
                    const aria = el.getAttribute('aria-label');
                    const title = el.getAttribute('title');
                    if (alt) valores.push(alt);
                    if (aria) valores.push(aria);
                    if (title) valores.push(title);
                });
                return valores;
            }"""
        )
        for valor in atributos:
            for match in PADRAO_EMPRESA.finditer(valor):
                mencoes.add(re.sub(r"\s+", " ", match.group(0)).strip())
    except Exception:
        pass

    return sorted(mencoes)


def _normalizar_tipo_passagem(bruto: str) -> str:
    """Padroniza o nome da classe/tipo da passagem."""
    chave = re.sub(r"\s+", " ", bruto.strip().lower()).replace("semi leito", "semi-leito")
    mapa = {
        "semi-leito": "Semi-leito",
        "leito": "Leito",
        "leito individual": "Leito Individual",
        "leito cama": "Leito Cama",
        "executivo": "Executivo",
        "convencional": "Convencional",
        "cama": "Cama",
    }
    return mapa.get(chave, bruto.strip().title())


PADRAO_DURACAO_HM = re.compile(r"\b(\d{1,2})\s*h\s*(\d{1,2})\s*m\b", re.IGNORECASE)
PADRAO_DURACAO_RELOGIO = re.compile(r"\b(\d{1,2}):(\d{2})\s*🕒")
PADRAO_LINHA_HORA = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d(?:\(\+\d+\))?$")
PADRAO_LOCAL_SAIDA = re.compile(
    r"rodovi[aá]ria|terminal|embarque|asa[\s-]?sul|smas",
    re.IGNORECASE,
)
PADRAO_DESTINO = re.compile(r"santa\s+rita|c[aá]ssia", re.IGNORECASE)


def _extrair_detalhes_card(card_text: str) -> dict[str, str]:
    """Extrai horário de saída, chegada, duração e local de saída de um card de viagem."""
    linhas = [re.sub(r"\s+", " ", linha).strip() for linha in card_text.splitlines()]
    linhas = [linha for linha in linhas if linha]

    duracao = ""
    match_hm = PADRAO_DURACAO_HM.search(card_text)
    if match_hm:
        duracao = f"{int(match_hm.group(1))}h {int(match_hm.group(2)):02d}m"
    else:
        match_relogio = PADRAO_DURACAO_RELOGIO.search(card_text)
        if match_relogio:
            duracao = f"{int(match_relogio.group(1))}h {int(match_relogio.group(2)):02d}m"

    horas_linhas: list[tuple[int, str]] = []
    for i, linha in enumerate(linhas):
        if PADRAO_LINHA_HORA.match(linha):
            horas_linhas.append((i, linha))

    horario_saida = horas_linhas[0][1] if horas_linhas else ""
    horario_chegada = horas_linhas[1][1] if len(horas_linhas) >= 2 else ""

    if not horario_saida:
        match_hora = PADRAO_HORARIO.search(card_text)
        horario_saida = match_hora.group(0) if match_hora else ""

    local_saida = ""
    for linha in linhas:
        if PADRAO_LOCAL_SAIDA.search(linha) and not PADRAO_DESTINO.search(linha):
            local_saida = linha
            break

    if not local_saida and horas_linhas:
        idx_proxima = horas_linhas[0][0] + 1
        if idx_proxima < len(linhas):
            candidato = linhas[idx_proxima]
            if not PADRAO_PRECO.search(candidato) and not PADRAO_DESTINO.search(candidato):
                local_saida = candidato

    return {
        "horario": horario_saida,
        "chegada": horario_chegada,
        "duracao": duracao,
        "local_saida": local_saida,
    }


def _extrair_opcoes_cards(page: Page, seletores: list[str]) -> list[dict[str, str]]:
    """Extrai tipo de passagem, horário, duração, local de saída e preço de cada card visível."""
    seletor_combinado = ", ".join(seletores)
    locator = page.locator(seletor_combinado)
    opcoes: list[dict[str, str]] = []
    vistas: set[tuple[str, str, str, str, str]] = set()

    try:
        total = locator.count()
    except Exception:
        return opcoes

    for idx in range(min(total, 50)):
        elemento = locator.nth(idx)
        try:
            if not elemento.is_visible():
                continue
            card_text = elemento.evaluate(
                """(btn) => {
                    let cur = btn;
                    let melhorTexto = '';
                    for (let i = 0; i < 12 && cur && cur.parentElement; i++) {
                        cur = cur.parentElement;
                        if (cur.tagName === 'MAIN' || cur.tagName === 'BODY') {
                            break;
                        }
                        const txt = cur.innerText || '';
                        const qtdPrecos = (txt.match(/R\\$\\s*\\d{1,3}(?:\\.\\d{3})*\\s*,\\s*\\d{2}/g) || []).length;
                        if (qtdPrecos > 1) {
                            break;
                        }
                        if (qtdPrecos === 1) {
                            melhorTexto = txt;
                            const temClasse = /semi[-\\s]?leito|leito|executivo|convencional|cama/i.test(txt);
                            const temHora = /\\b(?:[01]?\\d|2[0-3]):[0-5]\\d\\b/.test(txt);
                            const temDuracaoOuLocal = /rodovi[aá]ria|\\d{1,2}\\s*h\\s*\\d{1,2}\\s*m|🕒/i.test(txt);
                            if (temClasse && temHora && temDuracaoOuLocal) {
                                return txt;
                            }
                        }
                    }
                    return melhorTexto;
                }"""
            )
            if not card_text:
                continue

            precos_card = extrair_precos(card_text)
            if not precos_card:
                continue

            match_tipo = PADRAO_TIPO_PASSAGEM.search(card_text)
            tipo = _normalizar_tipo_passagem(match_tipo.group(1)) if match_tipo else "Padrão"
            detalhes = _extrair_detalhes_card(card_text)
            preco = precos_card[0]

            chave = (
                tipo,
                detalhes["horario"],
                detalhes["duracao"],
                detalhes["local_saida"],
                preco,
            )
            if chave not in vistas:
                vistas.add(chave)
                opcoes.append(
                    {
                        "tipo": tipo,
                        "horario": detalhes["horario"],
                        "chegada": detalhes["chegada"],
                        "duracao": detalhes["duracao"],
                        "local_saida": detalhes["local_saida"],
                        "preco": preco,
                    }
                )
        except Exception:
            continue

    return opcoes


def verificar_guanabara(browser: Browser, url: str) -> dict[str, Any]:
    """Verifica disponibilidade de passagens no site Viaje Guanabara."""
    resultado: dict[str, Any] = {
        "site": "Viaje Guanabara",
        "url": url,
        "disponivel": False,
        "empresa_detectada": False,
        "mencoes_empresa": [],
        "botoes_compra": [],
        "precos": [],
        "opcoes": [],
        "sem_resultados": False,
        "erro": None,
    }

    context = criar_contexto_navegador(browser)
    page = context.new_page()
    try:
        logger.info("[Viaje Guanabara] Acessando: %s", url)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        _aguardar_carregamento_dinamico(page, SELETORES_BOTAO_COMPRA_GUANABARA)

        texto_principal = _obter_texto_principal(page)
        botoes = _coletar_botoes_visiveis(page, SELETORES_BOTAO_COMPRA_GUANABARA)
        mencoes = _detectar_mencao_empresa(page, texto_principal)
        sem_resultados = bool(PADRAO_SEM_RESULTADOS.search(texto_principal))

        empresa_detectada = len(mencoes) > 0
        disponivel = len(botoes) > 0 and empresa_detectada and not sem_resultados
        opcoes = _extrair_opcoes_cards(page, SELETORES_BOTAO_COMPRA_GUANABARA) if disponivel else []
        precos = extrair_precos(texto_principal) if disponivel else []

        resultado.update(
            {
                "disponivel": disponivel,
                "empresa_detectada": empresa_detectada,
                "mencoes_empresa": mencoes,
                "botoes_compra": botoes,
                "precos": precos,
                "opcoes": opcoes,
                "sem_resultados": sem_resultados,
            }
        )

        logger.info(
            "[Viaje Guanabara] Disponível=%s | Botões=%d | Empresa=%s | Opções=%s | Preços=%s | SemResultados=%s",
            disponivel,
            len(botoes),
            mencoes,
            [f"{o['tipo']} ({o['preco']})" for o in opcoes],
            precos,
            sem_resultados,
        )
    except Exception as exc:
        resultado["erro"] = str(exc)
        logger.error("[Viaje Guanabara] Erro ao verificar: %s", exc)
    finally:
        context.close()

    return resultado


def verificar_clickbus(browser: Browser, url: str) -> dict[str, Any]:
    """Verifica disponibilidade de passagens da Rápido Federal / Guanabara na ClickBus."""
    resultado: dict[str, Any] = {
        "site": "ClickBus",
        "url": url,
        "disponivel": False,
        "empresa_detectada": False,
        "mencoes_empresa": [],
        "botoes_compra": [],
        "precos": [],
        "opcoes": [],
        "sem_resultados": False,
        "erro": None,
    }

    context = criar_contexto_navegador(browser)
    page = context.new_page()
    try:
        logger.info("[ClickBus] Acessando: %s", url)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        _aguardar_carregamento_dinamico(page, SELETORES_BOTAO_COMPRA_CLICKBUS)

        texto_principal = _obter_texto_principal(page)
        botoes = _coletar_botoes_visiveis(page, SELETORES_BOTAO_COMPRA_CLICKBUS)
        mencoes = _detectar_mencao_empresa(page, texto_principal)
        sem_resultados = bool(PADRAO_SEM_RESULTADOS.search(texto_principal))

        empresa_detectada = len(mencoes) > 0
        disponivel = len(botoes) > 0 and empresa_detectada and not sem_resultados
        opcoes = _extrair_opcoes_cards(page, SELETORES_BOTAO_COMPRA_CLICKBUS) if disponivel else []
        precos = extrair_precos(texto_principal) if disponivel else []

        resultado.update(
            {
                "disponivel": disponivel,
                "empresa_detectada": empresa_detectada,
                "mencoes_empresa": mencoes,
                "botoes_compra": botoes,
                "precos": precos,
                "opcoes": opcoes,
                "sem_resultados": sem_resultados,
            }
        )

        logger.info(
            "[ClickBus] Disponível=%s | Botões=%d | Empresa=%s | Opções=%s | Preços=%s | SemResultados=%s",
            disponivel,
            len(botoes),
            mencoes,
            [f"{o['tipo']} ({o['preco']})" for o in opcoes],
            precos,
            sem_resultados,
        )
    except Exception as exc:
        resultado["erro"] = str(exc)
        logger.error("[ClickBus] Erro ao verificar: %s", exc)
    finally:
        context.close()

    return resultado


DEFAULT_ARQUIVO_ESTADO = "estado_monitoramento.json"


def _normalizar_empresas(mencoes: list[str]) -> str:
    """Remove duplicados ignorando maiúsculas/minúsculas e formata a lista de viações."""
    unicas: list[str] = []
    vistas: set[str] = set()
    for nome in mencoes:
        chave = nome.strip().lower()
        if chave and chave not in vistas:
            vistas.add(chave)
            unicas.append(nome.strip().title())
    return ", ".join(unicas) if unicas else "Rápido Federal / Guanabara"


def carregar_estado(caminho: str) -> dict[str, Any]:
    """Carrega o estado anterior salvo em JSON para evitar alertas repetidos."""
    arquivo = Path(caminho)
    if not arquivo.exists():
        return {}
    try:
        dados = json.loads(arquivo.read_text(encoding="utf-8"))
        return dados if isinstance(dados, dict) else {}
    except Exception as exc:
        logger.warning("Não foi possível ler %s (%s). Iniciando estado vazio.", caminho, exc)
        return {}


def salvar_estado(caminho: str, estado: dict[str, Any]) -> None:
    """Persiste o estado atual das verificações em JSON."""
    try:
        Path(caminho).write_text(
            json.dumps(estado, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("Não foi possível salvar %s: %s", caminho, exc)


def deve_enviar_alerta(
    resultado: dict[str, Any],
    estado_site_anterior: dict[str, Any] | None,
    notificar_sempre: bool = False,
) -> tuple[bool, str]:
    """Decide se um alerta deve ser enviado com base no histórico anterior do site."""
    if not resultado.get("disponivel"):
        return False, "indisponivel"

    if notificar_sempre or not estado_site_anterior:
        return True, "nova"

    url_mudou = estado_site_anterior.get("url") != resultado.get("url")
    estava_disponivel = bool(estado_site_anterior.get("disponivel", False))
    precos_anteriores = estado_site_anterior.get("precos", [])
    precos_atuais = resultado.get("precos", [])

    if url_mudou or not estava_disponivel:
        return True, "nova"

    if precos_atuais != precos_anteriores:
        return True, "preco_alterado"

    return False, "ja_notificado"


def _extrair_data_viagem(url: str) -> str:
    """Extrai a data da viagem da URL (departure_date ou departureDate) no formato DD/MM/AAAA."""
    try:
        params = parse_qs(urlparse(url).query)
        bruta = (params.get("departure_date") or params.get("departureDate") or [""])[0].strip()
        if not bruta:
            match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", url)
            bruta = match.group(1) if match else ""
        if bruta:
            return datetime.strptime(bruta, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        pass
    return "Não identificada"


def enviar_alerta_telegram(resultado: dict[str, Any], motivo: str = "nova") -> bool:
    """Envia um alerta formatado em HTML para o Telegram informando a disponibilidade."""
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        logger.warning(
            "TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID não configurados. Alerta não enviado para %s.",
            resultado["site"],
        )
        return False

    site = html.escape(resultado["site"])
    url = html.escape(resultado["url"], quote=True)
    data_viagem = html.escape(_extrair_data_viagem(resultado["url"]))
    empresas = html.escape(_normalizar_empresas(resultado.get("mencoes_empresa", [])))
    opcoes = resultado.get("opcoes", [])
    precos = resultado.get("precos", [])
    horario_verificacao = datetime.now(FUSO_BRASILIA).strftime("%d/%m/%Y %H:%M:%S")
    titulo = "💰 <b>PREÇOS ATUALIZADOS!</b>" if motivo == "preco_alterado" else "🚌 <b>PASSAGEM LIBERADA!</b>"

    linhas_viagem: list[str] = []
    if opcoes:
        locais = {op.get("local_saida", "") for op in opcoes if op.get("local_saida")}
        horarios = {
            (op.get("horario", ""), op.get("chegada", ""), op.get("duracao", ""))
            for op in opcoes
            if op.get("horario") or op.get("duracao")
        }
        mesma_viagem = len(locais) <= 1 and len(horarios) <= 1

        if mesma_viagem:
            if locais:
                linhas_viagem.append(
                    f"🚏 <b>Local de saída:</b> <b><u>{html.escape(next(iter(locais)))}</u></b>"
                )
            if horarios:
                saida, chegada, duracao = next(iter(horarios))
                partes_horario = []
                if saida and chegada:
                    partes_horario.append(f"⏰ <b>Horário:</b> {html.escape(saida)} ➔ {html.escape(chegada)}")
                elif saida:
                    partes_horario.append(f"⏰ <b>Saída:</b> {html.escape(saida)}")
                if duracao:
                    partes_horario.append(f"⏱️ <b>Duração:</b> {html.escape(duracao)}")
                if partes_horario:
                    linhas_viagem.append(" | ".join(partes_horario))

            linhas_opcoes = [
                f"  • <b>{html.escape(op['tipo'])}</b> — <b>{html.escape(op['preco'])}</b>"
                for op in opcoes
            ]
            bloco_valores = "💺 <b>Tipos e preços:</b>\n" + "\n".join(linhas_opcoes)
        else:
            grupos: dict[str, list[dict[str, str]]] = {}
            for op in opcoes:
                chave_local = op.get("local_saida") or "Local não informado"
                grupos.setdefault(chave_local, []).append(op)

            blocos_grupos: list[str] = []
            for local_nome, itens in grupos.items():
                linhas_grupo = [f"🚏 <b><u>{html.escape(local_nome)}</u></b>"]
                for op in itens:
                    tipo = html.escape(op["tipo"])
                    preco = html.escape(op["preco"])
                    extras = []
                    if op.get("horario") and op.get("chegada"):
                        extras.append(f"{html.escape(op['horario'])} ➔ {html.escape(op['chegada'])}")
                    elif op.get("horario"):
                        extras.append(html.escape(op["horario"]))
                    if op.get("duracao"):
                        extras.append(html.escape(op["duracao"]))
                    detalhe = f" ({' • '.join(extras)})" if extras else ""
                    linhas_grupo.append(f"  • <b>{tipo}</b> — <b>{preco}</b>{detalhe}")
                blocos_grupos.append("\n".join(linhas_grupo))

            bloco_valores = "💺 <b>Opções por local de saída:</b>\n\n" + "\n\n".join(blocos_grupos)
    else:
        precos_formatados = html.escape(", ".join(precos)) if precos else "Não identificado na listagem"
        bloco_valores = f"💰 <b>Preços encontrados:</b> {precos_formatados}"

    bloco_info_viagem = ("\n".join(linhas_viagem) + "\n") if linhas_viagem else ""

    mensagem = (
        f"{titulo}\n\n"
        f"🌐 <b>Site:</b> {site}\n"
        f"📍 <b>Trecho:</b> Brasília - DF ➔ Santa Rita de Cássia - BA\n"
        f"📅 <b>Data da viagem:</b> {data_viagem}\n"
        f"🏢 <b>Viação:</b> {empresas}\n"
        f"{bloco_info_viagem}"
        f"{bloco_valores}\n"
        f"🕒 <b>Verificado em:</b> {horario_verificacao}\n\n"
        f"👉 <a href=\"{url}\">Abrir página de compra</a>"
    )

    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": mensagem,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(endpoint, json=payload, timeout=20)
        response.raise_for_status()
        logger.info("Alerta enviado no Telegram com sucesso para %s.", resultado["site"])
        return True
    except requests.RequestException as exc:
        logger.error("Falha ao enviar alerta no Telegram: %s", exc)
        return False


def executar_verificacao(
    url_guanabara: str,
    url_clickbus: str,
    enviar_telegram: bool = True,
    arquivo_estado: str | None = None,
    notificar_sempre: bool = False,
) -> list[dict[str, Any]]:
    """Executa uma rodada completa de verificação nos dois sites com proteção anti-spam."""
    resultados: list[dict[str, Any]] = []
    caminho_estado = arquivo_estado or os.getenv("ARQUIVO_ESTADO", DEFAULT_ARQUIVO_ESTADO).strip()
    estado = carregar_estado(caminho_estado)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        try:
            res_guanabara = verificar_guanabara(browser, url_guanabara)
            resultados.append(res_guanabara)

            res_clickbus = verificar_clickbus(browser, url_clickbus)
            resultados.append(res_clickbus)
        finally:
            browser.close()

    agora_iso = datetime.now(FUSO_BRASILIA).isoformat(timespec="seconds")

    for res in resultados:
        site = res["site"]
        estado_anterior = estado.get(site)

        if res["erro"]:
            logger.warning("Verificação de %s finalizou com erro: %s", site, res["erro"])
            continue

        if res["disponivel"]:
            enviar, motivo = deve_enviar_alerta(res, estado_anterior, notificar_sempre=notificar_sempre)
            if enviar:
                logger.info(
                    "PASSAGEM DISPONÍVEL em %s (%s)! Preços: %s",
                    site,
                    motivo,
                    res["precos"],
                )
                alerta_ok = True
                if enviar_telegram:
                    alerta_ok = enviar_alerta_telegram(res, motivo=motivo)
                if alerta_ok:
                    estado[site] = {
                        "disponivel": True,
                        "url": res["url"],
                        "precos": res["precos"],
                        "atualizado_em": agora_iso,
                    }
            else:
                logger.info(
                    "Alerta já enviado anteriormente para %s com os mesmos preços %s. Pulando para evitar spam.",
                    site,
                    res["precos"],
                )
        else:
            logger.info("Nenhuma passagem disponível no momento em %s.", site)
            estado[site] = {
                "disponivel": False,
                "url": res["url"],
                "precos": [],
                "atualizado_em": agora_iso,
            }

    salvar_estado(caminho_estado, estado)
    return resultados


def main() -> None:
    load_dotenv()

    url_guanabara = os.getenv("URL_GUANABARA", "").strip() or DEFAULT_URL_GUANABARA
    url_clickbus = os.getenv("URL_CLICKBUS", "").strip() or DEFAULT_URL_CLICKBUS
    modo_loop = os.getenv("MODO_LOOP_LOCAL", "false").strip().lower() in {"1", "true", "sim", "yes"}
    notificar_sempre = os.getenv("NOTIFICAR_SEMPRE", "false").strip().lower() in {"1", "true", "sim", "yes"}
    intervalo_segundos = int(os.getenv("INTERVALO_SEGUNDOS", "600"))
    arquivo_estado = os.getenv("ARQUIVO_ESTADO", DEFAULT_ARQUIVO_ESTADO).strip()

    if modo_loop:
        logger.info("Iniciando monitoramento em loop local (intervalo: %ds).", intervalo_segundos)
        while True:
            executar_verificacao(
                url_guanabara,
                url_clickbus,
                enviar_telegram=True,
                arquivo_estado=arquivo_estado,
                notificar_sempre=notificar_sempre,
            )
            logger.info("Aguardando %d segundos para a próxima verificação...", intervalo_segundos)
            time.sleep(intervalo_segundos)
    else:
        logger.info("Executando verificação única (modo GitHub Actions / execução pontual).")
        executar_verificacao(
            url_guanabara,
            url_clickbus,
            enviar_telegram=True,
            arquivo_estado=arquivo_estado,
            notificar_sempre=notificar_sempre,
        )


if __name__ == "__main__":
    main()
