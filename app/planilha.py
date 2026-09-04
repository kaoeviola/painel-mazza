from pathlib import Path
from typing import Any
import re

import gspread
from gspread.exceptions import APIError, SpreadsheetNotFound, WorksheetNotFound

from app.config import BASE_DIR


CREDENCIAIS_PATH = BASE_DIR / "credenciais" / "google_service_account.json"


class PlanilhaErro(Exception):
    def __init__(self, motivo: str, client_email: str | None = None):
        super().__init__(motivo)
        self.motivo = motivo
        self.client_email = client_email


def verificar_acesso(planilha_id: str) -> tuple[bool, str | None, str | None, str | None]:
    if not planilha_id or planilha_id.startswith("PLACEHOLDER"):
        return False, None, "nao_configurado", _client_email()

    try:
        planilha = _cliente().open_by_key(planilha_id)
        return True, planilha.title, None, _client_email()
    except SpreadsheetNotFound:
        return False, None, "nao_encontrada", _client_email()
    except APIError as exc:
        return False, None, _motivo_api(exc), _client_email()
    except FileNotFoundError:
        return False, None, "credenciais_nao_configuradas", None


def colar_leads(planilha_id: str, leads: list[dict[str, Any]]) -> dict[str, Any]:
    if not leads:
        return {
            "escritos": 0,
            "linha_inicial": None,
            "formula_coluna_b": "sem_leads",
        }

    cliente = _cliente()
    planilha = cliente.open_by_key(planilha_id)
    aba = planilha.sheet1
    linha_inicial = _primeira_linha_vazia_coluna_a(aba)
    linha_final = linha_inicial + len(leads) - 1

    formula_info = _inspecionar_formula_coluna_b(aba)

    valores_a = [[lead.get("nome", "")] for lead in leads]
    valores_c = [[lead.get("atendente", "")] for lead in leads]
    valores_i = [[lead.get("numero_bruto", "")] for lead in leads]

    aba.batch_update(
        [
            {"range": f"A{linha_inicial}:A{linha_final}", "values": valores_a},
            {"range": f"C{linha_inicial}:C{linha_final}", "values": valores_c},
            {"range": f"I{linha_inicial}:I{linha_final}", "values": valores_i},
        ],
        value_input_option="USER_ENTERED",
    )

    if formula_info["modo"] == "formula_por_linha":
        _copiar_formula_coluna_b(planilha, aba, linha_inicial, linha_final)

    return {
        "escritos": len(leads),
        "linha_inicial": linha_inicial,
        "linha_final": linha_final,
        "formula_coluna_b": formula_info["modo"],
        "amostra_formula_b": formula_info["amostra"],
    }


def telefones_existentes(planilha_id: str) -> set[str]:
    cliente = _cliente()
    planilha = cliente.open_by_key(planilha_id)
    aba = planilha.sheet1
    valores_b = aba.col_values(2)
    valores_i = aba.col_values(9)
    telefones: set[str] = set()
    for valor in valores_b[1:] + valores_i[1:]:
        normalizado = _normalizar_telefone_planilha(valor)
        if normalizado:
            telefones.add(normalizado)
    return telefones


def normalizar_telefone_planilha(valor: str) -> str:
    return _normalizar_telefone_planilha(valor)


def ler_daily_cap(planilha_id: str) -> int:
    try:
        valor = _cliente().open_by_key(planilha_id).worksheet("config").acell("B2").value
    except Exception as exc:
        raise PlanilhaErro(_mensagem_erro_config(exc)) from exc

    try:
        daily_cap = int(str(valor).strip())
    except (TypeError, ValueError) as exc:
        raise PlanilhaErro("O valor de config!B2 na planilha nao e um inteiro valido.") from exc

    if not 1 <= daily_cap <= 500:
        raise PlanilhaErro("O valor de config!B2 na planilha deve estar entre 1 e 500.")
    return daily_cap


def atualizar_daily_cap(planilha_id: str, daily_cap: int) -> None:
    try:
        _cliente().open_by_key(planilha_id).worksheet("config").update_acell("B2", str(daily_cap))
    except Exception as exc:
        raise PlanilhaErro(_mensagem_erro_config(exc)) from exc


def _cliente() -> gspread.Client:
    return gspread.service_account(filename=str(CREDENCIAIS_PATH))


def _normalizar_telefone_planilha(valor: str) -> str:
    digitos = re.sub(r"\D", "", valor or "")
    if not digitos:
        return ""
    if not digitos.startswith("55"):
        digitos = "55" + digitos
    return digitos


def _client_email() -> str | None:
    try:
        dados = CREDENCIAIS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None

    import json

    return json.loads(dados).get("client_email")


def _motivo_api(exc: APIError) -> str:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    texto = str(exc).lower()
    if status in {401, 403} or "permission" in texto or "forbidden" in texto:
        return "erro_permissao"
    if status == 404 or "not found" in texto:
        return "nao_encontrada"
    return "erro_api"


def _mensagem_erro_config(exc: Exception) -> str:
    if isinstance(exc, FileNotFoundError):
        return "As credenciais da planilha nao estao configuradas."
    if isinstance(exc, WorksheetNotFound):
        return "A aba 'config' nao foi encontrada na planilha Disparo Mazza."
    if isinstance(exc, SpreadsheetNotFound):
        return "A planilha Disparo Mazza nao foi encontrada ou a service account nao tem acesso."
    if isinstance(exc, APIError) and _motivo_api(exc) == "erro_permissao":
        return "A service account nao tem permissao para acessar a planilha Disparo Mazza."
    return "Nao foi possivel acessar a planilha Disparo Mazza. Tente novamente."


def _primeira_linha_vazia_coluna_a(aba) -> int:
    valores = aba.col_values(1)
    for indice in range(2, len(valores) + 1):
        if not valores[indice - 1].strip():
            return indice
    return max(len(valores) + 1, 2)


def _inspecionar_formula_coluna_b(aba) -> dict[str, str | None]:
    valores = aba.get("B2:B5", value_render_option="FORMULA")
    formulas = [linha[0] for linha in valores if linha and linha[0].startswith("=")]
    amostra = formulas[0] if formulas else None

    if any("ARRAYFORMULA" in formula.upper() for formula in formulas):
        return {"modo": "arrayformula", "amostra": amostra}
    if formulas:
        return {"modo": "formula_por_linha", "amostra": amostra}
    return {"modo": "sem_formula_detectada", "amostra": None}


def _copiar_formula_coluna_b(planilha, aba, linha_inicial: int, linha_final: int) -> None:
    if linha_inicial <= 2:
        origem_linha = 2
    else:
        origem_linha = linha_inicial - 1

    body = {
        "requests": [
            {
                "copyPaste": {
                    "source": {
                        "sheetId": aba.id,
                        "startRowIndex": origem_linha - 1,
                        "endRowIndex": origem_linha,
                        "startColumnIndex": 1,
                        "endColumnIndex": 2,
                    },
                    "destination": {
                        "sheetId": aba.id,
                        "startRowIndex": linha_inicial - 1,
                        "endRowIndex": linha_final,
                        "startColumnIndex": 1,
                        "endColumnIndex": 2,
                    },
                    "pasteType": "PASTE_FORMULA",
                    "pasteOrientation": "NORMAL",
                }
            }
        ]
    }
    planilha.batch_update(body)
