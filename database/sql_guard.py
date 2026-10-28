"""Разбор скрипта на пакеты (GO) и оценка опасности команд.

Анализ консервативный (лучше лишний раз спросить): всё, что не похоже на чтение,
требует режима «разрешить изменения» и подтверждения. Динамический SQL (EXEC(...),
sp_executesql) не анализируется и считается опасным. Это защита от случайностей,
а не замена прав доступа на сервере: для отчётов используйте учётную запись только на чтение.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum

from .sql_lexer import NORMAL, Token, lex, lex_line


class RiskLevel(IntEnum):
    READ = 0
    MODIFY = 1
    DANGEROUS = 2


RISK_LABELS = {RiskLevel.READ: "только чтение", RiskLevel.MODIFY: "изменяет данные или схему",
               RiskLevel.DANGEROUS: "потенциально опасно"}


@dataclass(frozen=True)
class Batch:
    text: str
    start_offset: int
    start_line: int


@dataclass(frozen=True)
class Finding:
    level: RiskLevel
    reason: str
    snippet: str
    offset: int  # позиция в исходном скрипте


@dataclass
class Analysis:
    batches: list[Batch] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    has_use: bool = False

    @property
    def level(self) -> RiskLevel:
        return max((f.level for f in self.findings), default=RiskLevel.READ)

    @property
    def reasons(self) -> list[str]:
        out: list[str] = []
        for f in self.findings:
            if f.reason not in out:
                out.append(f.reason)
        return out

    @property
    def is_read_only(self) -> bool:
        return self.level is RiskLevel.READ and not self.errors


# ---------------------------------------------------------------- пакеты GO

def split_batches(sql: str) -> tuple[list[Batch], list[str]]:
    lines = sql.split("\n")
    batches: list[Batch] = []
    errors: list[str] = []
    state, offset = NORMAL, 0
    cur: list[str] = []
    cur_off, cur_line = 0, 0

    def flush() -> None:
        text = "\n".join(cur)
        if any(t.kind != "comment" for t in lex(text).tokens):
            batches.append(Batch(text, cur_off, cur_line))

    for ln, line in enumerate(lines):
        toks, new_state = lex_line(line, state)
        code = [t for t in toks if t.kind != "comment"]
        is_go = (state == NORMAL and code and code[0].kind == "word" and code[0].upper == "GO"
                 and (len(code) == 1 or (len(code) == 2 and code[1].kind == "number")))
        if is_go:
            if len(code) == 2 and int(code[1].text) != 1:
                errors.append(f"Строка {ln + 1}: «GO n» с повтором не поддерживается — "
                              "выполните пакет нужное число раз вручную.")
            flush()
            cur, cur_off, cur_line = [], offset + len(line) + 1, ln + 1
        else:
            cur.append(line)
        state = new_state
        offset += len(line) + 1
    flush()
    return batches, errors


# --------------------------------------------------------------- анализ

SAFE_PROCS = frozenset("""
sp_help sp_helptext sp_helpindex sp_columns sp_tables sp_spaceused sp_who sp_who2 sp_databases sp_fkeys sp_pkeys
sp_stored_procedures sp_helpdb sp_helpconstraint sp_depends sp_statistics
""".split())
DANGEROUS_PROCS = frozenset("xp_cmdshell sp_executesql sp_configure sp_oacreate sp_oamethod sp_addlinkedserver "
                            "sp_addsrvrolemember sp_addrolemember".split())
KNOWN_STARTERS = frozenset("""
SELECT WITH DECLARE SET PRINT IF ELSE WHILE BEGIN END USE RAISERROR THROW WAITFOR RETURN COMMIT ROLLBACK SAVE GOTO
FETCH OPEN CLOSE DEALLOCATE BREAK CONTINUE TRY CATCH INSERT UPDATE DELETE MERGE DROP TRUNCATE ALTER CREATE EXEC EXECUTE
GRANT REVOKE DENY BACKUP RESTORE SHUTDOWN KILL RECONFIGURE DBCC BULK READTEXT WRITETEXT UPDATETEXT ENABLE DISABLE
CHECKPOINT GO
""".split())
STOP_WORDS = frozenset("""
SELECT INSERT UPDATE DELETE MERGE DROP TRUNCATE ALTER CREATE EXEC EXECUTE DECLARE IF WHILE BEGIN END RETURN PRINT
WITH GRANT REVOKE DENY
""".split())
SKIP_NAME = frozenset("TABLE IF EXISTS INTO FROM TOP PERCENT VIEW OR ALTER UNIQUE CLUSTERED NONCLUSTERED".split())


def _snippet(text: str, start: int) -> str:
    line = text[start:].split("\n", 1)[0].strip()
    return line[:110] + ("…" if len(line) > 110 else "")


def _name_after(toks: list[Token], i: int) -> str:
    """Первое «имя» после ключевого слова (пропуская TABLE/INTO/FROM/TOP (n) и т. п.)."""
    j = i + 1
    while j < len(toks):
        t = toks[j]
        if t.kind == "op" and t.text == "(":  # TOP (n) — пропустить скобки
            depth = 0
            while j < len(toks):
                if toks[j].kind == "op" and toks[j].text == "(":
                    depth += 1
                elif toks[j].kind == "op" and toks[j].text == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            j += 1
            continue
        if t.kind == "word" and t.upper in SKIP_NAME:
            j += 1
            continue
        if t.kind == "number":
            j += 1
            continue
        if t.kind in ("word", "ident", "var"):
            return t.text
        return ""
    return ""


def _is_temp(name: str) -> bool:
    """#temp и @табличная_переменная живут в сеансе и не меняют постоянные данные (##global — нет)."""
    return (name.startswith("#") and not name.startswith("##")) or name.startswith("@")


def _clean_name(name: str) -> str:
    return name.strip("[]\"").split(".")[-1].lower()


def _has_where(toks: list[Token], i: int) -> tuple[bool, bool]:
    """(есть WHERE на верхнем уровне, условие тривиально истинно)."""
    depth = 0
    j = i + 1
    while j < len(toks):
        t = toks[j]
        if t.kind == "op":
            if t.text == "(":
                depth += 1
            elif t.text == ")":
                depth = max(0, depth - 1)
            elif t.text == ";" and depth == 0:
                return False, False
        elif t.kind == "word" and depth == 0:
            u = t.upper
            if u == "WHERE":
                nxt = [x.text for x in toks[j + 1:j + 4]]
                return True, nxt == ["1", "=", "1"]
            if u in STOP_WORDS and j > i + 1:
                return False, False
        j += 1
    return False, False


def _in_permission_stmt(toks: list[Token], i: int) -> bool:
    j = i - 1
    while j >= 0:
        t = toks[j]
        if t.kind == "op" and t.text == ";":
            return False
        if t.kind == "word":
            if t.upper in ("GRANT", "REVOKE", "DENY"):
                return True
            if t.upper in ("ON", "TO"):
                return False
        j -= 1
    return False


def _analyze_batch(batch: Batch, analysis: Analysis) -> None:
    text = batch.text
    toks = [t for t in lex(text).tokens if t.kind != "comment"]
    n = len(toks)

    def add(level: RiskLevel, reason: str, tok: Token) -> None:
        analysis.findings.append(Finding(level, reason, _snippet(text, tok.start), batch.start_offset + tok.start))

    def prev_kw(i: int) -> str:
        return toks[i - 1].upper if i > 0 and toks[i - 1].kind == "word" else ""

    def next_tok(i: int) -> Token | None:
        return toks[i + 1] if i + 1 < n else None

    # вызов процедуры без EXEC (первое слово пакета или после ';')
    for i, t in enumerate(toks):
        starts = i == 0 or (toks[i - 1].kind == "op" and toks[i - 1].text == ";")
        if starts and t.kind == "word" and t.upper not in KNOWN_STARTERS and not t.text.startswith("#"):
            nxt = next_tok(i)
            if nxt is None or nxt.kind != "op" or nxt.text not in ("=", "."):
                add(RiskLevel.MODIFY, f"Возможный вызов процедуры «{t.text}» без EXEC — может изменять данные", t)

    for i, t in enumerate(toks):
        if t.kind != "word":
            continue
        kw, prev = t.upper, prev_kw(i)
        if kw == "USE":
            analysis.has_use = True
        elif kw == "DROP":
            if not _is_temp(_name_after(toks, i)):
                add(RiskLevel.DANGEROUS, "DROP безвозвратно удаляет объект", t)
        elif kw == "TRUNCATE":
            if not _is_temp(_name_after(toks, i)):
                add(RiskLevel.DANGEROUS, "TRUNCATE удаляет ВСЕ строки таблицы без возможности отката построчно", t)
        elif kw == "CREATE":
            if not _is_temp(_name_after(toks, i)):
                add(RiskLevel.MODIFY, "CREATE изменяет схему базы данных", t)
        elif kw == "ALTER":
            if not _is_temp(_name_after(toks, i)):
                add(RiskLevel.MODIFY, "ALTER изменяет схему базы данных", t)
        elif kw == "INSERT" and not _in_permission_stmt(toks, i):
            if not _is_temp(_name_after(toks, i)):
                add(RiskLevel.MODIFY, "INSERT добавляет строки", t)
        elif kw == "UPDATE" and prev != "ON" and not _in_permission_stmt(toks, i):
            nxt = next_tok(i)
            if nxt is not None and nxt.kind == "word" and nxt.upper == "STATISTICS":
                add(RiskLevel.MODIFY, "UPDATE STATISTICS обновляет статистику", t)
            elif prev == "THEN":
                add(RiskLevel.MODIFY, "MERGE обновляет строки", t)
            elif not _is_temp(_name_after(toks, i)):
                has_where, tautology = _has_where(toks, i)
                if not has_where or tautology:
                    add(RiskLevel.DANGEROUS, "UPDATE без условия WHERE изменит ВСЕ строки"
                        if not has_where else "UPDATE с условием «1 = 1» изменит ВСЕ строки", t)
                else:
                    add(RiskLevel.MODIFY, "UPDATE изменяет строки", t)
        elif kw == "DELETE" and prev != "ON" and not _in_permission_stmt(toks, i):
            if prev == "THEN":
                add(RiskLevel.DANGEROUS, "MERGE удаляет строки", t)
            elif not _is_temp(_name_after(toks, i)):
                has_where, tautology = _has_where(toks, i)
                if not has_where or tautology:
                    add(RiskLevel.DANGEROUS, "DELETE без условия WHERE удалит ВСЕ строки", t)
                else:
                    add(RiskLevel.DANGEROUS, "DELETE удаляет строки — проверьте условие WHERE", t)
        elif kw == "MERGE":
            add(RiskLevel.MODIFY, "MERGE изменяет данные (вставка, обновление, удаление)", t)
        elif kw in ("EXEC", "EXECUTE"):
            nxt = next_tok(i)
            if nxt is not None and nxt.kind == "op" and nxt.text == "(":
                add(RiskLevel.DANGEROUS, "Динамический SQL (EXEC(...)) не анализируется и может сделать что угодно", t)
                continue
            if nxt is not None and nxt.kind == "word" and nxt.upper == "AS":
                add(RiskLevel.MODIFY, "EXECUTE AS меняет контекст безопасности", t)
                continue
            j = i + 1
            if j < n and toks[j].kind == "var" and j + 1 < n and toks[j + 1].text == "=":
                j += 2
            name = _clean_name(toks[j].text) if j < n and toks[j].kind in ("word", "ident") else ""
            if name in DANGEROUS_PROCS:
                add(RiskLevel.DANGEROUS, f"Процедура {name} небезопасна или выполняет динамический код", t)
            elif name in SAFE_PROCS:
                continue
            else:
                add(RiskLevel.MODIFY, f"EXEC {name or '…'}: процедура может изменять данные", t)
        elif kw in ("GRANT", "REVOKE", "DENY"):
            add(RiskLevel.MODIFY, f"{kw} меняет разрешения", t)
        elif kw == "RESTORE":
            add(RiskLevel.DANGEROUS, "RESTORE перезаписывает базу данных", t)
        elif kw in ("SHUTDOWN", "KILL"):
            add(RiskLevel.DANGEROUS, f"{kw} влияет на работу сервера", t)
        elif kw in ("BACKUP", "RECONFIGURE", "DBCC", "BULK", "WRITETEXT", "UPDATETEXT"):
            add(RiskLevel.MODIFY, f"{kw} изменяет данные, файлы или настройки сервера", t)
        elif kw in ("OPENROWSET", "OPENQUERY", "OPENDATASOURCE"):
            add(RiskLevel.MODIFY, f"{kw} обращается к внешнему источнику", t)
        elif kw in ("ENABLE", "DISABLE") and (nx := next_tok(i)) is not None and nx.upper == "TRIGGER":
            add(RiskLevel.MODIFY, f"{kw} TRIGGER меняет поведение базы данных", t)
        elif kw == "SELECT":
            depth, j = 0, i + 1
            while j < n:
                u = toks[j]
                if u.kind == "op" and u.text == "(":
                    depth += 1
                elif u.kind == "op" and u.text == ")":
                    depth = max(0, depth - 1)
                elif u.kind == "op" and u.text == ";" and depth == 0:
                    break
                elif u.kind == "word" and depth == 0:
                    if u.upper == "INTO":
                        if not _is_temp(_name_after(toks, j)):
                            add(RiskLevel.MODIFY, "SELECT INTO создаёт новую таблицу", u)
                        break
                    if u.upper in ("FROM",) or (u.upper in STOP_WORDS and u.upper != "SELECT"):
                        break
                j += 1


def analyze(sql: str) -> Analysis:
    analysis = Analysis()
    analysis.batches, analysis.errors = split_batches(sql)
    for b in analysis.batches:
        _analyze_batch(b, analysis)
    return analysis


def describe(analysis: Analysis, limit: int = 12) -> str:
    """Текст для окна подтверждения."""
    lines = [f"Уровень: {RISK_LABELS[analysis.level]}", ""]
    for f in analysis.findings[:limit]:
        lines.append(f"• {f.reason}\n    {f.snippet}")
    if len(analysis.findings) > limit:
        lines.append(f"… и ещё {len(analysis.findings) - limit}")
    return "\n".join(lines)
