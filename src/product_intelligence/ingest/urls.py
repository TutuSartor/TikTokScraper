"""Normalização conservadora de URLs de fonte (ARCHITECTURE.md §5).

Objetivo: a mesma página colada de jeitos diferentes vira a mesma chave, SEM remover nada
que possa identificar o conteúdo. Por isso só são removidos parâmetros de rastreamento de
marketing bem conhecidos; parâmetros específicos do TikTok (ex.: `_r`, `_t`, `is_from_webapp`)
são preservados, porque não sabemos com segurança quais identificam o item.
"""

import re
from urllib.parse import unquote, urlsplit, urlunsplit

MAX_URL_LENGTH = 2048

# Parâmetros exclusivamente de rastreamento de campanha/clique.
_TRACKING_PARAMS = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid"}
_TRACKING_PREFIXES = ("utm_",)
_DEFAULT_PORTS = {"http": 80, "https": 443}


class InvalidURL(ValueError):
    pass


def _is_tracking(name: str) -> bool:
    lowered = name.lower()
    return lowered in _TRACKING_PARAMS or lowered.startswith(_TRACKING_PREFIXES)


def normalize_url(raw: str) -> str:
    """Devolve a URL canônica ou levanta InvalidURL.

    - exige http/https e host;
    - esquema e host em minúsculas; remove porta padrão, `www.` NÃO é removido;
    - remove âncoras simples e rastreamento; preserva rotas de aplicações (#/..., #!/..., #?...);
    - preserva ordem e codificação dos parâmetros restantes, inclusive repetidos;
    - caminho vazio vira "/"; o restante do caminho é preservado como veio.
    """
    value = (raw or "").strip()
    if not value:
        raise InvalidURL("URL vazia")
    if any(ch.isspace() for ch in value):
        raise InvalidURL("URL contém espaços")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise InvalidURL("URL contém caracteres de controle")
    if re.search(r"%(?![0-9A-Fa-f]{2})", value):
        raise InvalidURL("URL contém escape percentual inválido")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise InvalidURL(f"URL inválida: {exc}") from exc

    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise InvalidURL("URL deve começar com http:// ou https://")
    host = (parts.hostname or "").lower()
    if not host or "." not in host:
        raise InvalidURL("URL sem domínio válido")
    if parts.username is not None or parts.password is not None:
        raise InvalidURL("URL não pode conter usuário/senha")

    netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    # Reordenar valores repetidos pode mudar qual id a fonte considera primeiro.
    # Recodificar também pode invalidar links assinados ou unir identificadores diferentes.
    query = "&".join(
        segment for segment in parts.query.split("&")
        if not _is_tracking(unquote(segment.partition("=")[0]))
    )
    path = parts.path or "/"

    fragment = parts.fragment if parts.fragment.startswith(("/", "!/", "?")) else ""
    normalized = urlunsplit((scheme, netloc, path, query, fragment))
    if len(normalized) > MAX_URL_LENGTH:
        raise InvalidURL(f"URL maior que {MAX_URL_LENGTH} caracteres")
    return normalized
