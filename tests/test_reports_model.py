import datetime as dt
import unittest
from decimal import Decimal

from reports.charts import ChartNotApplicable, chart_data, render_chart
from reports.formatting import NBSP, Formatter, looks_like_identifier
from reports.model import (Aggregate, Block, ColumnSpec, DataSource, Filter, Metric, Param, ReportTemplate,
                           SortSpec, validate_template)
from reports.query_builder import QueryBuildError, build_procedure_call, build_query, build_table_query
from reports.stats import (aggregate_by, column_stats, conclusions, dataset_stats, infer_kind, metric_value,
                           time_series)
from reports.styles import STYLES, get_style
from reports.variables import (ParamError, coerce_param, convert_named_params, find_named_params, render_text)


class ModelTests(unittest.TestCase):
    def tpl(self, **kw):
        t = ReportTemplate(name="T", sources=[DataSource(id="s", name="S", kind="sql", sql="SELECT 1")],
                           blocks=[Block(id="b", type="table", source="s")])
        for k, v in kw.items():
            setattr(t, k, v)
        return t

    def test_roundtrip_json(self):
        t = self.tpl()
        t.id = "t-fixed"
        t.blocks[0].columns = [ColumnSpec("a", total="sum")]
        t.sources[0].filters = [Filter("x", "=", ":p")]
        t.params = [Param("p")]
        t2 = ReportTemplate.from_dict(t.to_dict())
        self.assertEqual(t2.to_dict(), t.to_dict())
        self.assertEqual(validate_template(t2), [])

    def test_unknown_keys_ignored_and_id_generated(self):
        t = ReportTemplate.from_dict({"name": "X", "future_field": 1, "blocks": [{"id": "b", "type": "text", "zzz": 2}]})
        self.assertTrue(t.id)
        self.assertEqual(t.blocks[0].type, "text")

    def test_six_styles(self):
        self.assertEqual(set(STYLES), {"business", "academic", "minimal", "analytic", "technical", "financial"})
        self.assertEqual(get_style("nope").id, "business")

    def test_validation_catches_problems(self):
        cases = {
            "изменяет": self.tpl(sources=[DataSource(id="s", kind="sql", sql="DELETE FROM t")]),
            "не объявлен": self.tpl(sources=[DataSource(id="s", kind="sql", sql="SELECT :zz")]),
            "один пакет": self.tpl(sources=[DataSource(id="s", kind="sql", sql="SELECT 1\nGO\nSELECT 2")]),
            "источник": self.tpl(blocks=[Block(id="b", type="table", source="missing")]),
            "стиль": self.tpl(style="neon"),
            "акцента": self.tpl(accent="red"),
            "повторяется": self.tpl(sources=[DataSource(id="s", kind="sql", sql="SELECT 1"),
                                            DataSource(id="s", kind="sql", sql="SELECT 2")]),
            "Параметр": self.tpl(params=[Param("1bad")]),
            "оси X": self.tpl(blocks=[Block(id="b", type="chart", source="s", y=["a"])]),
            "подтвердите": self.tpl(sources=[DataSource(id="s", kind="procedure", procedure="dbo.p")]),
        }
        for key, t in cases.items():
            errs = " ".join(validate_template(t))
            self.assertIn(key, errs, key)

    def test_valid_template_with_param_and_cast(self):
        t = self.tpl(params=[Param("p")],
                     sources=[DataSource(id="s", kind="sql", sql="SELECT geography::Point(1,2,4326), :p")])
        self.assertEqual(validate_template(t), [])


class VariablesTests(unittest.TestCase):
    def test_named_params(self):
        sql, vals = convert_named_params("SELECT * FROM t WHERE d>=:a AND n=':x' AND g=geography::Point(1,2) AND d<=:a",
                                         {"a": 5})
        self.assertEqual(sql, "SELECT * FROM t WHERE d>=? AND n=':x' AND g=geography::Point(1,2) AND d<=?")
        self.assertEqual(vals, [5, 5])
        self.assertEqual(find_named_params("SELECT :b, :a, :b -- :c"), ["b", "a"])
        with self.assertRaises(ParamError):
            convert_named_params("SELECT :a", {})

    def test_values_never_inlined(self):
        sql, vals = convert_named_params("SELECT * FROM t WHERE n = :n", {"n": "x'; DROP TABLE t;--"})
        self.assertNotIn("DROP", sql)
        self.assertEqual(vals, ["x'; DROP TABLE t;--"])

    def test_coerce(self):
        self.assertEqual(coerce_param(Param("a", type="int"), " 42 "), 42)
        self.assertEqual(coerce_param(Param("a", type="decimal"), "1,5"), Decimal("1.5"))
        self.assertEqual(coerce_param(Param("a", type="date"), "05.03.2024"), dt.date(2024, 3, 5))
        self.assertEqual(coerce_param(Param("a", type="date"), "2024-03-05"), dt.date(2024, 3, 5))
        self.assertIs(coerce_param(Param("a", type="bool"), "да"), True)
        self.assertIsNone(coerce_param(Param("a", type="int"), ""))
        self.assertEqual(coerce_param(Param("a", type="int", default="7"), None), 7)
        for p, raw in ((Param("a", type="int"), "x"), (Param("a", type="date"), "32.13.2024"),
                       (Param("a", type="int", required=True), ""), (Param("a", type="bool"), "maybe")):
            with self.assertRaises(ParamError):
                coerce_param(p, raw)

    def test_render_text(self):
        out, missing = render_text("Привет, {{ name }}! {{unknown}}", {"name": "Мир"})
        self.assertEqual((out, missing), ("Привет, Мир! {{unknown}}", ["unknown"]))


class QueryBuilderTests(unittest.TestCase):
    def test_table_query_quotes_and_parameters(self):
        s = DataSource(id="a", kind="table", database="Sa]les", schema="dbo", table="Orders", columns=["Id", "Tot]al"],
                       filters=[Filter("Status", "=", ":st"), Filter("Id", "in", "1;2;3"), Filter("x", "is_null"),
                                Filter("Name", "like", "A%")],
                       order_by=[SortSpec("Total", True)])
        sql, vals = build_table_query(s, {"st": "o'pen"}, 500)
        self.assertEqual(sql, "SELECT TOP (500) [Id], [Tot]]al] FROM [Sa]]les].[dbo].[Orders] WHERE [Status] = ? "
                              "AND [Id] IN (?, ?, ?) AND [x] IS NULL AND [Name] LIKE ? ORDER BY [Total] DESC")
        self.assertEqual(vals, ["o'pen", "1", "2", "3", "A%"])
        self.assertNotIn("o'pen", sql)

    def test_injection_in_identifiers_is_neutralised(self):
        s = DataSource(id="a", kind="table", table="t]; DROP TABLE x;--", columns=["a]; DELETE FROM t;--"])
        sql, _ = build_table_query(s, {})
        self.assertIn("[t]]; DROP TABLE x;--]", sql)

    def test_group_by_aggregates(self):
        s = DataSource(id="a", kind="table", table="T", group_by=["Region"],
                       aggregates=[Aggregate("Total", "sum", "Сумма"), Aggregate("*", "count", "Кол"),
                                   Aggregate("Cust", "count_distinct", "Клиентов")])
        sql, _ = build_table_query(s, {})
        self.assertEqual(sql, "SELECT [Region], SUM([Total]) AS [Сумма], COUNT(*) AS [Кол], COUNT(DISTINCT [Cust]) "
                              "AS [Клиентов] FROM [dbo].[T] GROUP BY [Region]")

    def test_errors(self):
        with self.assertRaises(QueryBuildError):
            build_table_query(DataSource(id="a", kind="table"), {})
        with self.assertRaises(QueryBuildError):
            build_table_query(DataSource(id="a", kind="table", table="t", filters=[Filter("a", "in", "")]), {})
        with self.assertRaises(ParamError):
            build_table_query(DataSource(id="a", kind="table", table="t", filters=[Filter("a", "=", ":missing")]), {})

    def test_procedure_and_dispatch(self):
        s = DataSource(id="p", kind="procedure", procedure="rep.GetSales", proc_params={"year": ":y", "x": "lit"})
        self.assertEqual(build_procedure_call(s, {"y": 2024}), ("EXEC [rep].[GetSales] @year = ?, @x = ?", [2024, "lit"]))
        self.assertEqual(build_query(DataSource(id="q", kind="sql", sql="SELECT :a"), {"a": 1}), ("SELECT ?", [1]))


class FormattingTests(unittest.TestCase):
    def test_ru_numbers(self):
        f = Formatter("ru", "₽")
        self.assertEqual(f.number(1234567.891, 2), f"1{NBSP}234{NBSP}567,89")
        self.assertEqual(f.number(Decimal("-1234.5")), f"−1{NBSP}234,5")
        self.assertEqual(f.number(5), "5")
        self.assertEqual(f.number(0.1 + 0.2), "0,3")
        self.assertEqual(f.number(2.5), "2,5")
        self.assertEqual(f.money(1234.5), f"1{NBSP}234,50{NBSP}₽")
        self.assertEqual(f.percent(0.256), f"25,6{NBSP}%")
        self.assertEqual(f.number(1234, 0, grouping=False), "1234")

    def test_en_numbers(self):
        f = Formatter("en")
        self.assertEqual(f.number(1234567.891, 2), "1,234,567.89")
        self.assertEqual(f.number(1234, 0, grouping=False), "1234")
        self.assertEqual(f.percent(0.5), "50.0%")

    def test_values(self):
        f = Formatter("ru")
        self.assertEqual(f.value(True), "Да")
        self.assertEqual(f.value(None), "")
        self.assertEqual(f.value(dt.date(2024, 3, 5)), "05.03.2024")
        self.assertEqual(f.value(dt.datetime(2024, 3, 5, 14, 30)), "05.03.2024 14:30")
        self.assertEqual(f.value(dt.datetime(2024, 3, 5, 0, 0), dates_only=True), "05.03.2024")
        self.assertEqual(f.value(b"\x01\xab"), "0x01AB")
        self.assertEqual(f.value(3.0, "integer"), "3")
        self.assertTrue(f.value("x" * 5000).endswith("…"))

    def test_identifier_detection(self):
        for n in ("Id", "customer_id", "ID", "Код", "Год", "OrderId", "Year", "Номер"):
            self.assertTrue(looks_like_identifier(n), n)
        for n in ("Сумма", "Total", "Name", "Количество"):
            self.assertFalse(looks_like_identifier(n), n)


class StatsTests(unittest.TestCase):
    def test_infer_kind(self):
        d = dt.date(2024, 1, 1)
        self.assertEqual(infer_kind([1, 2, None]), "integer")
        self.assertEqual(infer_kind([1, 2.5]), "numeric")
        self.assertEqual(infer_kind([Decimal("1.1"), 2]), "numeric")
        self.assertEqual(infer_kind([d, None]), "date")
        self.assertEqual(infer_kind([dt.datetime(2024, 1, 1), dt.datetime(2024, 1, 2)]), "date")
        self.assertEqual(infer_kind([dt.datetime(2024, 1, 1, 5)]), "datetime")
        self.assertEqual(infer_kind(["a", None]), "text")
        self.assertEqual(infer_kind([True, False]), "bool")
        self.assertEqual(infer_kind([b"x"]), "binary")
        self.assertEqual(infer_kind([None, None]), "empty")
        self.assertEqual(infer_kind([1, "a"]), "mixed")

    def test_numeric_stats_exact_and_id_skipped(self):
        s = column_stats("Сумма", [Decimal("0.1"), Decimal("0.2"), None, Decimal("0.3")])
        self.assertEqual((s.count, s.nulls, s.total, s.minimum, s.maximum), (3, 1, Decimal("0.6"), Decimal("0.1"), Decimal("0.3")))
        self.assertEqual(s.mean, Decimal("0.2"))
        ident = column_stats("customer_id", [1, 2, 3])
        self.assertIsNone(ident.total)
        self.assertTrue(ident.skipped_reason)
        self.assertEqual(ident.maximum, 3)

    def test_text_and_date_stats_have_no_sum(self):
        t = column_stats("Регион", ["a", "b", "a"])
        self.assertIsNone(t.total)
        self.assertEqual(t.top[0], ("a", 2))
        d = column_stats("Дата", [dt.date(2024, 1, 1), dt.date(2024, 5, 1)])
        self.assertIsNone(d.total)
        self.assertEqual((d.minimum, d.maximum), (dt.date(2024, 1, 1), dt.date(2024, 5, 1)))

    def test_aggregates_series_and_metrics(self):
        rows = [("a", 1, dt.date(2024, 1, 5)), ("b", 2, dt.date(2024, 1, 20)), ("a", 3, dt.date(2024, 4, 1)), ("b", None, dt.date(2025, 1, 1))]
        self.assertEqual(dict(aggregate_by(rows, 0, 1, "sum")), {"a": 4, "b": 2})
        self.assertEqual(dict(aggregate_by(rows, 0, None, "count")), {"a": 2, "b": 2})
        pts = time_series(rows[:3], 2, 1, "sum")
        self.assertEqual(pts, [("2024-01", 3), ("2024-04", 3)])                      # период > 62 дней — по месяцам
        self.assertEqual([p[0] for p in time_series(rows[:2], 2, 1, "sum")], ["2024-01-05", "2024-01-20"])  # короткий — по дням
        self.assertEqual([p[0] for p in time_series(rows, 2, 1, "sum")], ["2024-01", "2024-04", "2025-01"])
        long_rows = [("a", 1, dt.date(2022, 5, 1)), ("a", 2, dt.date(2023, 5, 1)), ("a", 4, dt.date(2025, 5, 1))]
        self.assertEqual(time_series(long_rows, 2, 1, "sum"), [("2022", 1), ("2023", 2), ("2025", 4)])  # > 2 лет — по годам
        cols = ["k", "v", "d"]
        self.assertEqual(metric_value(cols, rows, Metric(op="sum", column="v")), 6)
        self.assertEqual(metric_value(cols, rows, Metric(op="distinct", column="k")), 2)
        self.assertEqual(metric_value(cols, rows, Metric(op="ratio", column="v", column2="v")), Decimal(1))
        self.assertIsNone(metric_value(cols, rows, Metric(op="sum", column="nope")))

    def test_conclusions_only_facts(self):
        f = Formatter("ru")
        rows = [("a", 10, dt.date(2024, 1, 1)), ("a", 30, dt.date(2024, 2, 1)), ("b", 20, dt.date(2024, 3, 1))]
        text = " ".join(conclusions("Продажи", ["Регион", "Сумма", "Дата"], rows, False, f))
        self.assertIn("получено записей — 3", text)
        self.assertIn("сумма — 60", text)
        self.assertIn("максимум — 30", text)
        self.assertIn("«a» (2 зап.", text)
        for forbidden in ("рост", "снизил", "вырос", "тенденц", "причин"):
            self.assertNotIn(forbidden, text)
        trunc = " ".join(conclusions("S", ["a"], [(1,)], True, f))
        self.assertIn("не полностью", trunc)
        self.assertEqual(len(conclusions("S", ["a"], [], False, f)), 1)


class ChartTests(unittest.TestCase):
    COLS = ["Регион", "Сумма", "Дата"]
    ROWS = [("Север", 100.5, dt.date(2024, 1, 5)), ("Юг", 250, dt.date(2024, 2, 5)),
            ("Север", 50, dt.date(2024, 3, 9)), ("Восток", 75, dt.date(2024, 3, 9))]

    def test_bar_pie_line_render_png(self):
        f, st = Formatter("ru"), get_style("analytic")
        for kind, x in (("bar", "Регион"), ("pie", "Регион"), ("line", "Дата")):
            d = chart_data(kind, self.COLS, self.ROWS, x, ["Сумма"], "sum", 10, f)
            png = render_chart(d, kind, "Тест", st, f)
            self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
            self.assertGreater(len(png), 3000)

    def test_not_applicable_cases(self):
        f = Formatter("ru")
        cases = [("line", "Регион", ["Сумма"], "текст"), ("bar", "Регион", ["Регион"], "не числовой"),
                 ("pie", "Дата", ["Сумма"], "дат"), ("bar", "Нет", ["Сумма"], "нет столбца")]
        for kind, x, ys, text in cases:
            with self.assertRaises(ChartNotApplicable) as ctx:
                chart_data(kind, self.COLS, self.ROWS, x, ys, "sum", 10, f)
            self.assertIn(text, str(ctx.exception))
        with self.assertRaises(ChartNotApplicable):
            chart_data("bar", self.COLS, [], "Регион", ["Сумма"], "sum", 10, f)
        with self.assertRaises(ChartNotApplicable):
            chart_data("pie", self.COLS, [("a", -1, None), ("b", 2, None)], "Регион", ["Сумма"], "sum", 10, f)

    def test_pie_merges_tail_and_count_agg(self):
        f = Formatter("ru")
        rows = [(f"k{i}", i + 1, None) for i in range(15)]
        d = chart_data("pie", self.COLS, rows, "Регион", ["Сумма"], "sum", 5, f)
        self.assertEqual((len(d.labels), d.labels[-1], d.other_merged), (6, "Прочее", 10))
        d2 = chart_data("bar", self.COLS, self.ROWS, "Регион", [], "count", 10, f)
        self.assertEqual(dict(zip(d2.labels, d2.series["Количество"])), {"Север": 2.0, "Юг": 1.0, "Восток": 1.0})


if __name__ == "__main__":
    unittest.main()
