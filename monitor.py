"""Monitor the PSF grants page and notify APyB through GitHub Discussions."""

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup, Comment

SOURCE_URL = "https://www.python.org/psf/grants/"
USER_AGENT = "psf-grant-news-check/1.0 (daily PSF grants monitor)"


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_content(html):
    """Keep article text, block boundaries and link destinations, not site chrome."""
    soup = BeautifulSoup(html, "html.parser")
    article = soup.select_one(".main-content article.text")
    if article is None:
        raise ValueError("Artigo da PSF não encontrado; verifique o seletor do scraper.")
    for comment in article.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()
    for element in article.select("script, style, nav, footer, [hidden], [aria-hidden='true']"):
        element.decompose()
    # The table of contents duplicates headings and changes when anchors are renamed.
    toc = article.find(id="table-of-contents")
    if toc is not None:
        listing = toc.find_next_sibling()
        if listing is not None and listing.name in {"ul", "ol"}:
            listing.decompose()
        toc.decompose()
    for text in article.find_all(string=True):
        text.replace_with(re.sub(r"\s+", " ", str(text)))
    for link in article.find_all("a", href=True):
        href = link["href"].strip()
        if href and not href.startswith("#"):
            link.append(f" ({urljoin(SOURCE_URL, href)})")
    for element in article.find_all(["p", "div", "h1", "h2", "h3", "h4", "h5", "h6",
                                     "li", "ul", "ol", "tr", "blockquote", "br"]):
        element.insert_before("\n")
        element.insert_after("\n")
    for cell in article.find_all(["td", "th"]):
        cell.insert_after(" | ")
    lines = [" ".join(line.split()) for line in article.get_text().splitlines()]
    content = "\n".join(line for line in lines if line)
    if len(content) < 500 or not re.search(r"\bgrants?\b", content, re.IGNORECASE):
        raise ValueError("Conteúdo vazio ou inesperado; a referência foi preservada.")
    return content


def fetch_content():
    request = Request(SOURCE_URL, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=30) as response:
        if response.headers.get_content_type() != "text/html":
            raise ValueError("A PSF não retornou HTML.")
        return extract_content(response.read().decode("utf-8"))


class GitHub:
    def __init__(self, token, repository, category):
        if not token:
            raise ValueError("Configure o secret DISCUSSIONS_TOKEN.")
        self.token = token
        self.owner, self.name = repository.split("/", 1)
        self.category = category

    def graphql(self, query, variables):
        request = Request(
            "https://api.github.com/graphql",
            data=json.dumps({"query": query, "variables": variables}).encode(),
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        # Do not retry mutations: a timeout may happen after GitHub creates the post.
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
        if result.get("errors") or not result.get("data"):
            raise ValueError(f"GitHub GraphQL falhou: {result.get('errors', 'sem dados')}")
        return result["data"]

    def destination(self):
        result = self.graphql("""
            query($owner: String!, $name: String!) {
              repository(owner: $owner, name: $name) {
                id
                discussionCategories(first: 100) { nodes { id name } }
              }
            }
        """, {"owner": self.owner, "name": self.name})["repository"]
        if result is None:
            raise ValueError("Repositório de destino não encontrado.")
        categories = result["discussionCategories"]["nodes"]
        for category in categories:
            if category["name"] == self.category:
                return result["id"], category["id"]
        raise ValueError(f"Categoria {self.category!r} não encontrada. Disponíveis: "
                         + ", ".join(category["name"] for category in categories))

    def find_existing(self, marker):
        # Enumerate instead of using search, whose indexing can lag behind creation.
        cursor = None
        while True:
            result = self.graphql("""
                query($owner: String!, $name: String!, $cursor: String) {
                  repository(owner: $owner, name: $name) {
                    discussions(first: 100, after: $cursor,
                      orderBy: {field: CREATED_AT, direction: DESC}) {
                      nodes { body url }
                      pageInfo { hasNextPage endCursor }
                    }
                  }
                }
            """, {"owner": self.owner, "name": self.name, "cursor": cursor})
            connection = result["repository"]["discussions"]
            for discussion in connection["nodes"]:
                if marker in discussion["body"]:
                    return discussion["url"]
            if not connection["pageInfo"]["hasNextPage"]:
                return None
            cursor = connection["pageInfo"]["endCursor"]

    def publish(self, title, body, marker):
        existing = self.find_existing(marker)
        if existing:
            return existing
        repository_id, category_id = self.destination()
        result = self.graphql("""
            mutation($input: CreateDiscussionInput!) {
              createDiscussion(input: $input) { discussion { url } }
            }
        """, {"input": {"repositoryId": repository_id, "categoryId": category_id,
                         "title": title, "body": body}})
        return result["createDiscussion"]["discussion"]["url"]


def discussion_body(before, after, marker, observed_at):
    diff = "\n".join(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile="Versão anterior", tofile="Versão atual", lineterm="", n=2,
    ))
    # Keep comfortably below GitHub's body limit, including multibyte characters.
    truncated = len(diff.encode("utf-8")) > 45000
    diff = diff.encode("utf-8")[:45000].decode("utf-8", errors="ignore")
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", diff)), default=0))
    note = "\nO diff foi truncado; consulte a página e o histórico de snapshots.\n" if truncated else ""
    return (
        f"Foi detectada uma alteração no [programa de grants da PSF]({SOURCE_URL}).\n\n"
        f"Verificação: {observed_at} (UTC).\n\n"
        "Confira o conteúdo atualizado para avaliar o impacto em pedidos de apoio da comunidade. "
        "O monitor compara texto e links; não faz uma avaliação semântica das mudanças.\n\n"
        "### Alterações detectadas\n\n"
        f"Linhas com `-` foram removidas; linhas com `+` foram adicionadas.\n\n"
        f"{fence}diff\n{diff}\n{fence}\n{note}\n{marker}\n"
    )


def load_state(path):
    if not path.exists():
        return None
    state = json.loads(path.read_text(encoding="utf-8"))
    if (state.get("version") != 1 or state.get("source") != SOURCE_URL
            or not isinstance(state.get("content"), str)
            or not isinstance(state.get("revision"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", state["revision"])
            or state.get("hash") != digest(state["content"])):
        raise ValueError("Snapshot inválido; restaure-o pelo histórico da branch monitor-state.")
    return state


def check(path, content, github=None, dry_run=False):
    previous = load_state(path)
    if previous and previous["content"] == content:
        print("Sem alterações relevantes.")
        return
    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    revision = digest((previous["revision"] if previous else "baseline") + "\n" + content)
    state = {"version": 1, "source": SOURCE_URL, "content": content,
             "hash": digest(content), "revision": revision, "observed_at": observed_at}
    if previous:
        marker = f"<!-- psf-grants-change:{revision} -->"
        body = discussion_body(previous["content"], content, marker, observed_at)
        if dry_run:
            print(body)
            return
        if github is None:
            raise ValueError("Cliente GitHub necessário para publicar a mudança.")
        state["discussion_url"] = github.publish(
            f"Atualização no programa de grants da PSF — {observed_at[:10]}", body, marker,
        )
        print(f"Discussion: {state['discussion_url']}")
    else:
        print("Primeira execução: referência inicial, sem criar discussion.")
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path(".local-state/snapshot.json"))
    parser.add_argument("--dry-run", action="store_true", help="Não publica nem grava estado.")
    parser.add_argument("--html", type=Path, help="Usa HTML local para testes offline.")
    args = parser.parse_args()
    content = extract_content(args.html.read_text(encoding="utf-8")) if args.html else fetch_content()
    github = None if args.dry_run else GitHub(
        os.environ.get("DISCUSSIONS_TOKEN", ""),
        os.environ.get("DISCUSSIONS_REPOSITORY", "apyb/comunidade"),
        os.environ.get("DISCUSSIONS_CATEGORY", "Geral"),
    )
    check(args.state, content, github, args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, URLError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        sys.exit(1)
