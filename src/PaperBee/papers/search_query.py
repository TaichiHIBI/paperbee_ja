"""検索クエリ（findpapers 互換の ``[term] AND ([a] OR [b]) AND NOT [c]`` 形式）の解釈と変換。

設定ファイルの ``query_pubmed_arxiv`` / ``query_biorxiv`` を構文木にし、
PubMed（E-utilities）・arXiv API の検索式への変換と、bioRxiv 新着のタイトル・アブストラクトに対する
ローカル照合に使う。演算子は AND / OR / AND NOT（大文字）、優先順位は AND > OR。
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, List, Tuple, Union


@dataclass(frozen=True)
class Term:
    text: str


@dataclass(frozen=True)
class Group:
    op: str  # "AND" | "OR"
    children: Tuple["Node", ...]


@dataclass(frozen=True)
class Not:
    child: "Node"


Node = Union[Term, Group, Not]

_TOKEN_RE = re.compile(r"\[([^\]]*)\]|\(|\)|AND NOT\b|AND\b|OR\b|\S+")


def _tokenize(query: str) -> List[Tuple[str, str]]:
    tokens: List[Tuple[str, str]] = []
    for m in _TOKEN_RE.finditer(" ".join(query.split())):
        raw = m.group(0)
        if m.group(1) is not None:
            text = m.group(1).strip()
            if not text:
                raise ValueError(f"空の検索語があります: {query!r}")
            tokens.append(("TERM", text))
        elif raw in ("(", ")", "AND", "OR", "AND NOT"):
            tokens.append((raw, raw))
        else:
            raise ValueError(f"検索語は [ ] で囲み、演算子は AND / OR / AND NOT（大文字）で書いてください: {raw!r}")
    return tokens


class _Parser:
    def __init__(self, tokens: List[Tuple[str, str]]) -> None:
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> str:
        return self.tokens[self.pos][0] if self.pos < len(self.tokens) else ""

    def take(self, kind: str) -> str:
        if self.peek() != kind:
            found = self.peek() or "クエリの終わり"
            raise ValueError(f"{kind} が必要な位置に {found} があります")
        value = self.tokens[self.pos][1]
        self.pos += 1
        return value

    def parse_or(self) -> Node:
        children = [self.parse_and()]
        while self.peek() == "OR":
            self.take("OR")
            children.append(self.parse_and())
        return children[0] if len(children) == 1 else Group("OR", tuple(children))

    def parse_and(self) -> Node:
        children = [self.parse_operand()]
        while self.peek() in ("AND", "AND NOT"):
            negate = self.take(self.peek()) == "AND NOT"
            operand = self.parse_operand()
            children.append(Not(operand) if negate else operand)
        return children[0] if len(children) == 1 else Group("AND", tuple(children))

    def parse_operand(self) -> Node:
        if self.peek() == "(":
            self.take("(")
            node = self.parse_or()
            self.take(")")
            return node
        return Term(self.take("TERM"))


def parse_query(query: str) -> Node:
    """クエリ文字列を構文木にする。書式が不正なら ValueError。"""
    parser = _Parser(_tokenize(query))
    node = parser.parse_or()
    if parser.pos != len(parser.tokens):
        raise ValueError(f"解釈できない箇所があります: {parser.tokens[parser.pos][1]!r}")
    return node


def _has_wildcard(text: str) -> bool:
    return "*" in text or "?" in text


def _render(node: Node, term: Callable[[str], str], and_not: str) -> str:
    if isinstance(node, Term):
        return term(node.text)
    if isinstance(node, Not):  # 構文上 AND NOT の右辺にしか現れず、下の Group の処理で展開される
        return _render(node.child, term, and_not)
    parts: List[str] = []
    for i, child in enumerate(node.children):
        inner = child.child if isinstance(child, Not) else child
        text = _render(inner, term, and_not)
        if isinstance(inner, Group):  # PubMed は左から順に評価するので、入れ子は必ず括弧で明示する
            text = f"({text})"
        if i > 0:
            text = f"{and_not if isinstance(child, Not) else node.op} {text}"
        parts.append(text)
    return " ".join(parts)


def to_pubmed(node: Node) -> str:
    """PubMed esearch の term にする。各語はタイトル・アブストラクト（[TIAB]）のフレーズ検索。"""

    def term(text: str) -> str:
        if _has_wildcard(text):
            return f"{text.replace(' ', '+')}[TIAB]"
        return f'"{text}"[TIAB]'

    return _render(node, term, "NOT")


def to_arxiv(node: Node) -> str:
    """arXiv API の search_query にする。各語はタイトルかアブストラクトでのフレーズ一致。

    ``all:`` は著者名・コメント欄まで対象になり、[AI] のような短い語で無関係な論文を拾うため使わない。
    arXiv の検索はハイフンを正しく扱わないので空白に置き換える（findpapers と同じ扱い）。
    """

    def term(text: str) -> str:
        text = " ".join(text.replace("-", " ").replace('"', " ").split())
        if _has_wildcard(text):  # ワイルドカードは引用符の中では効かない
            return f"(ti:{text} OR abs:{text})"
        return f'(ti:"{text}" OR abs:"{text}")'

    return _render(node, term, "ANDNOT")


def normalize_text(text: str) -> str:
    """照合用に小文字化し、アクセント・記号を除き、英数字以外を空白1つにまとめる。"""
    decomposed = unicodedata.normalize("NFKD", text)
    chars = (ch if ch.isalnum() else " " for ch in decomposed if not unicodedata.combining(ch))
    return " ".join("".join(chars).lower().split())


def _term_pattern(text: str) -> "re.Pattern[str]":
    """検索語を normalize_text 済みの本文に対する正規表現にする。

    語の境界で一致させ（[GEM] は gemcitabine に一致しない）、最後の語だけ複数形（-s, -es）も許す。
    ハイフンや記号は空白と同じ扱い（[single-cell] は "single cell" にも一致する）。
    """
    decomposed = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    pieces: List[str] = []
    has_literal = False
    pending_space = False
    for ch in decomposed.lower():
        if ch.isalnum() or ch in "*?":
            if pending_space and pieces:
                pieces.append(" ")
            pending_space = False
            if ch == "*":
                pieces.append(r"\w*")  # ワイルドカードは同じ語の中の前後とつなげる（[immun*therapy]）
            elif ch == "?":
                pieces.append(r"\w")
            else:
                pieces.append(re.escape(ch))
                has_literal = True
        else:  # 空白・ハイフン・記号は語の区切り（本文側も normalize_text で空白1つになっている）
            pending_space = True
    if not has_literal:
        return re.compile(r"(?!)")
    return re.compile(r"(?<!\w)" + "".join(pieces) + r"(?:s|es)?(?!\w)")


def compile_matcher(node: Node) -> Callable[[str], bool]:
    """構文木を「normalize_text 済みの本文が一致するか」を返す関数にする。"""
    if isinstance(node, Term):
        pattern = _term_pattern(node.text)
        return lambda text: pattern.search(text) is not None
    if isinstance(node, Not):
        inner = compile_matcher(node.child)
        return lambda text: not inner(text)
    matchers = [compile_matcher(child) for child in node.children]
    if node.op == "AND":
        return lambda text: all(m(text) for m in matchers)
    return lambda text: any(m(text) for m in matchers)
