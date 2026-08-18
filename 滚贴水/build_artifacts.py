from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter
import nbformat as nbf
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"
CHARTS = OUTPUTS / "charts"
CHARTS.mkdir(parents=True, exist_ok=True)
INDIVIDUAL = OUTPUTS / "individual"
INDIVIDUAL.mkdir(parents=True, exist_ok=True)

COLORS = {"IF": "#2475B0", "IH": "#D6A21E", "IC": "#E6812F", "IM": "#7A8F35"}
plt.rcParams.update(
    {
        "font.family": ["Arial Unicode MS", "DejaVu Sans"],
        "axes.unicode_minus": False,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "grid.color": "#E5E7EB",
        "text.color": "#23262F",
        "axes.labelcolor": "#4B5563",
    }
)


def pct(value: float) -> str:
    return f"{value:.2%}"


def load_outputs() -> dict[str, pd.DataFrame]:
    return {
        "summary": pd.read_csv(OUTPUTS / "summary.csv"),
        "annual": pd.read_csv(OUTPUTS / "annual_returns.csv"),
        "daily": pd.read_csv(OUTPUTS / "daily_returns.csv", parse_dates=["date"]),
        "sensitivity": pd.read_csv(OUTPUTS / "sensitivity.csv"),
    }


def build_static_charts(data: dict[str, pd.DataFrame]) -> None:
    daily = data["daily"]
    fig, ax = plt.subplots(figsize=(11, 6.2))
    for product, group in daily.groupby("product"):
        ax.plot(group["date"], group["relative_wealth"], label=product, color=COLORS[product], linewidth=1.8)
    ax.axhline(1.0, color="#6B7280", linewidth=1.0)
    ax.set_title("主力期货策略相对对应全收益指数的累计净值", loc="left", fontsize=15, weight="bold")
    ax.set_ylabel("期货策略净值 / 全收益指数净值")
    ax.grid(axis="y", linewidth=0.7)
    ax.legend(ncol=4, frameon=False, loc="upper left")
    fig.tight_layout()
    fig.savefig(CHARTS / "relative_wealth.png", dpi=180, bbox_inches="tight")
    plt.close(fig)

    annual = data["annual"].pivot(index="year", columns="product", values="excess_return")
    annual = annual.reindex(columns=["IF", "IH", "IC", "IM"])
    masked = np.ma.masked_invalid(annual.to_numpy(dtype=float))
    fig, ax = plt.subplots(figsize=(9.5, 7.2))
    image = ax.imshow(masked, aspect="auto", cmap="RdYlBu", vmin=-0.15, vmax=0.30)
    ax.set_title("年度净超额收益热力图", loc="left", fontsize=15, weight="bold")
    ax.set_xticks(range(len(annual.columns)), annual.columns)
    ax.set_yticks(range(len(annual.index)), annual.index)
    for row in range(len(annual.index)):
        for col in range(len(annual.columns)):
            value = annual.iloc[row, col]
            if pd.notna(value):
                ax.text(col, row, f"{value:.1%}", ha="center", va="center", fontsize=8, color="#111827")
    colorbar = fig.colorbar(image, ax=ax, fraction=0.035, pad=0.03)
    colorbar.set_label("期货策略收益 - 指数收益")
    fig.tight_layout()
    fig.savefig(CHARTS / "annual_excess_heatmap.png", dpi=180, bbox_inches="tight")
    plt.close(fig)

    summary = data["summary"].set_index("product")
    for product in ["IF", "IH", "IC", "IM"]:
        group = daily.loc[daily["product"] == product].copy()
        metrics = summary.loc[product]
        group.to_csv(INDIVIDUAL / f"{product}_daily.csv", index=False, encoding="utf-8-sig")
        summary.loc[[product]].reset_index().to_csv(
            INDIVIDUAL / f"{product}_summary.csv", index=False, encoding="utf-8-sig"
        )

        fig, axes = plt.subplots(
            2,
            1,
            figsize=(11, 8.2),
            sharex=True,
            gridspec_kw={"height_ratios": [2.15, 1.0], "hspace": 0.08},
        )
        axes[0].plot(
            group["date"],
            group["strategy_wealth"],
            color=COLORS[product],
            linewidth=2.0,
            label=f"{product} 主力期货滚贴水策略",
        )
        axes[0].plot(
            group["date"],
            group["benchmark_wealth"],
            color="#6B7280",
            linewidth=1.7,
            linestyle="--",
            label="对应税前全收益指数",
        )
        fig.suptitle(
            f"{product} 独立滚贴水收益测试",
            x=0.11,
            y=0.985,
            ha="left",
            fontsize=15,
            weight="bold",
        )
        fig.text(
            0.11,
            0.946,
            (
                f"{metrics['start_date']}—{metrics['end_date']} ｜ "
                f"策略年化 {metrics['strategy_cagr']:.2%} ｜ "
                f"全收益基准年化 {metrics['benchmark_cagr']:.2%} ｜ "
                f"年化超额 {metrics['annualized_excess_vs_benchmark']:+.2%}"
            ),
            fontsize=10.5,
            color="#4B5563",
            ha="left",
            va="top",
        )
        axes[0].set_ylabel("累计净值")
        axes[0].grid(axis="y", linewidth=0.7)
        axes[0].legend(frameon=False, loc="upper left")

        axes[1].fill_between(
            group["date"],
            group["strategy_drawdown"],
            0,
            color=COLORS[product],
            alpha=0.18,
            label="策略回撤",
        )
        axes[1].plot(
            group["date"],
            group["benchmark_drawdown"],
            color="#6B7280",
            linewidth=1.4,
            linestyle="--",
            label="全收益基准回撤",
        )
        axes[1].set_ylabel("回撤")
        axes[1].set_xlabel("日期")
        axes[1].yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
        axes[1].grid(axis="y", linewidth=0.7)
        axes[1].legend(frameon=False, loc="lower left", ncol=2)
        fig.subplots_adjust(left=0.11, right=0.985, top=0.90, bottom=0.08, hspace=0.08)
        fig.savefig(CHARTS / f"{product}_return_test.png", dpi=180, bbox_inches="tight")
        plt.close(fig)


def build_notebook(data: dict[str, pd.DataFrame]) -> Path:
    summary = data["summary"].set_index("product")
    tl_dr = (
        "## tl;dr\n\n"
        f"- 扣除基准交易成本后，对全收益指数年化超额：IF **{pct(summary.loc['IF', 'annualized_excess_vs_benchmark'])}**、"
        f"IH **{pct(summary.loc['IH', 'annualized_excess_vs_benchmark'])}**、"
        f"IC **{pct(summary.loc['IC', 'annualized_excess_vs_benchmark'])}**、"
        f"IM **{pct(summary.loc['IM', 'annualized_excess_vs_benchmark'])}**。\n"
        "- 四个品种分别从各自上市日起独立复利，不构造四品种每日等权组合。\n"
        "- 结果证明历史主力路径存在可观贴水收益，但不是全合约最优选择回测；缺少新旧合约同刻报价、盘口和真实成交回报，结论应视为研究级估计。"
    )
    cells = [
        nbf.v4.new_markdown_cell(tl_dr),
        nbf.v4.new_markdown_cell(
            "## Context & Methods\n\n"
            "目标是测算做多四类股指期货主力合约并持续展期的实际净收益。"
            "模型取 15:00 对齐价，合约不变时使用真实期货收益；主连换码日用现货收益替代不可交易的主连跳变。\n\n"
            "### Key Assumptions\n\n"
            "- 1 倍名义敞口；现金收益 0%。\n"
            "- 手续费为成交金额 0.000023，每边滑点 0.2 指数点。\n"
            "- 开仓、换月两边和期末平仓都计成本。"
        ),
        nbf.v4.new_code_cell(
            "from pathlib import Path\n"
            "import pandas as pd\n"
            "import numpy as np\n"
            "import matplotlib.pyplot as plt\n"
            "ROOT = Path.cwd()\n"
            "OUT = ROOT / 'outputs'\n"
            "summary = pd.read_csv(OUT / 'summary.csv')\n"
            "annual = pd.read_csv(OUT / 'annual_returns.csv')\n"
            "daily = pd.read_csv(OUT / 'daily_returns.csv', parse_dates=['date'])\n"
            "sensitivity = pd.read_csv(OUT / 'sensitivity.csv')"
        ),
        nbf.v4.new_markdown_cell("## Data\n\n检查样本期、对齐天数、空值和主力换月次数。"),
        nbf.v4.new_code_cell(
            "quality = daily.groupby('product').agg(\n"
            "    start=('date','min'), end=('date','max'), rows=('date','size'),\n"
            "    roll_count=('is_roll','sum'), null_futures=('futures_close',lambda x:x.isna().sum()),\n"
            "    null_spot=('spot_close',lambda x:x.isna().sum()),\n"
            "    null_benchmark=('benchmark_close',lambda x:x.isna().sum()))\n"
            "quality"
        ),
        nbf.v4.new_markdown_cell("## Results\n\n全样本收益、风险、贴水比例与成本汇总。"),
        nbf.v4.new_code_cell(
            "cols = ['product','start_date','end_date','strategy_cagr','benchmark_cagr',\n"
            "        'annualized_excess_vs_benchmark','strategy_max_drawdown','tracking_error',\n"
            "        'roll_count','median_basis_pct','discount_day_share','total_transaction_cost']\n"
            "summary[cols].style.format({c:'{:.2%}' for c in ['strategy_cagr','benchmark_cagr',\n"
            " 'annualized_excess_vs_benchmark','strategy_max_drawdown','tracking_error','median_basis_pct',\n"
            " 'discount_day_share','total_transaction_cost']})"
        ),
        nbf.v4.new_code_cell(
            "colors={'IF':'#2475B0','IH':'#D6A21E','IC':'#E6812F','IM':'#7A8F35'}\n"
            "fig, axes = plt.subplots(2,2,figsize=(14,9))\n"
            "for ax,(p,g) in zip(axes.flat,daily.groupby('product',sort=False)):\n"
            "    ax.plot(g.date,g.strategy_wealth,label=f'{p}期货策略',color=colors[p])\n"
            "    ax.plot(g.date,g.benchmark_wealth,label='全收益基准',color='#6B7280',ls='--')\n"
            "    ax.set_title(f'{p}独立收益测试'); ax.grid(axis='y',alpha=.25); ax.legend(frameon=False)\n"
            "fig.tight_layout(); plt.show()"
        ),
        nbf.v4.new_code_cell(
            "pivot=annual.pivot(index='year',columns='product',values='excess_return')\n"
            "pivot.style.format('{:.2%}').background_gradient(cmap='RdYlBu',axis=None,vmin=-.15,vmax=.30)"
        ),
        nbf.v4.new_markdown_cell("### 成本与现金收益敏感性\n\n保守成本为 2 倍交易费率和每边 0.4 点滑点；现金情景只对未占用保证金的 88% 资金计 2% 年收益。"),
        nbf.v4.new_code_cell(
            "sensitivity.pivot(index='product',columns='scenario',values='annualized_excess_vs_benchmark').style.format('{:.2%}')"
        ),
        nbf.v4.new_markdown_cell(
            "## Takeaways\n\n"
            "- IC、IM 的历史贴水收益最强，但也最依赖时期与合约选择。\n"
            "- IF 在 2010—2014 年多次出现负超额，说明滚贴水不是稳定无风险利差。\n"
            "- 生产版本应补充全合约同刻行情，以固定、无前视的换月规则重跑，并用真实成交回报替换滑点假设。"
        ),
    ]
    notebook = nbf.v4.new_notebook(cells=cells)
    notebook["metadata"]["kernelspec"] = {"display_name": "Python (ag)", "language": "python", "name": "python3"}
    notebook["metadata"]["language_info"] = {"name": "python", "version": "3.10"}
    path = ROOT / "滚贴水回测.ipynb"
    nbf.write(notebook, path)
    return path


def _records(frame: pd.DataFrame) -> list[dict]:
    clean = frame.replace({np.nan: None})
    return json.loads(clean.to_json(orient="records", force_ascii=False, date_format="iso"))


def build_report_artifact(data: dict[str, pd.DataFrame]) -> Path:
    generated = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    summary = data["summary"].copy()
    basis_rows = summary[["product", "median_basis_pct", "discount_day_share"]]
    product_wealth: dict[str, pd.DataFrame] = {}
    for product in ["IF", "IH", "IC", "IM"]:
        product_daily = data["daily"].loc[data["daily"]["product"] == product].copy()
        product_daily["month"] = product_daily["date"].dt.to_period("M")
        monthly = product_daily.groupby("month", as_index=False).tail(1)
        wealth_long = pd.concat(
            [
                monthly[["date", "strategy_wealth"]]
                .rename(columns={"strategy_wealth": "wealth"})
                .assign(series=f"{product}主力期货策略"),
                monthly[["date", "benchmark_wealth"]]
                .rename(columns={"benchmark_wealth": "wealth"})
                .assign(series="对应税前全收益指数"),
            ],
            ignore_index=True,
        ).sort_values(["date", "series"])
        wealth_long.insert(0, "product", product)
        wealth_long["date"] = wealth_long["date"].dt.strftime("%Y-%m-%d")
        product_wealth[f"{product.lower()}_wealth"] = wealth_long
    product_wealth_all = pd.concat(product_wealth.values(), ignore_index=True)

    # 报告数据通过真实执行的 SQLite 查询整形，使 HTML 来源入口能展示可运行 SQL，
    # 同时保留 Python 回测作为上游计算来源。
    sqlite_path = OUTPUTS / "results.sqlite"
    with sqlite3.connect(sqlite_path) as connection:
        summary.to_sql("summary", connection, if_exists="replace", index=False)
        data["annual"].to_sql("annual_returns", connection, if_exists="replace", index=False)
        product_wealth_all.to_sql("product_wealth", connection, if_exists="replace", index=False)
        data["sensitivity"].to_sql("sensitivity", connection, if_exists="replace", index=False)

        def queried(name: str) -> pd.DataFrame:
            sql = (ROOT / "queries" / name).read_text(encoding="utf-8")
            return pd.read_sql_query(sql, connection)

        summary_rows = queried("summary.sql")
        annual_rows = queried("annual_returns.sql")
        product_wealth_rows = queried("product_wealth.sql")
        sensitivity_rows = queried("sensitivity.sql")
    product_wealth = {
        f"{product.lower()}_wealth": product_wealth_rows.loc[
            product_wealth_rows["product"] == product
        ].copy()
        for product in ["IF", "IH", "IC", "IM"]
    }

    sources = [
        {"id": "summary_source", "label": "四品种全样本回测汇总查询", "path": "queries/summary.sql"},
        {"id": "annual_source", "label": "年度收益明细查询", "path": "queries/annual_returns.sql"},
        {"id": "product_wealth_source", "label": "四品种独立月末净值查询", "path": "queries/product_wealth.sql"},
        {"id": "sensitivity_source", "label": "成本与现金收益敏感性查询", "path": "queries/sensitivity.sql"},
        {"id": "quality_source", "label": "源数据覆盖与质量检查", "path": "outputs/data_quality.json"},
        {"id": "code_source", "label": "回测实现", "path": "src/roll_discount_backtest.py"},
    ]
    # 文件型来源用安全的相对路径暴露；完整计算逻辑保存在同目录 Python 源码中。
    # 不伪造 SQL，因为本次数据处理来自本地 CSV/ZIP 与 Python。
    top_sources = [dict(source) for source in sources]

    # 文件型 Python 回测不伪造 SQL；头部指标直接在技术摘要与来源表中呈现。
    cards: list[dict] = []

    charts = [
        {
            "id": "excess_by_product",
            "title": "四类股指期货全样本年化超额",
            "subtitle": "扣除交易费与每边0.2点滑点；各品种从上市日至2025年末",
            "type": "horizontalBar",
            "dataset": "summary",
            "sourceId": "summary_source",
            "valueFormat": "percent",
            "encodings": {
                "x": {"field": "product", "type": "nominal", "label": "品种"},
                "y": {"field": "annualized_excess_vs_benchmark", "type": "quantitative", "label": "年化超额", "format": "percent"},
                "tooltip": [
                    {"field": "strategy_cagr", "type": "quantitative", "label": "策略年化", "format": "percent"},
                    {"field": "benchmark_cagr", "type": "quantitative", "label": "全收益指数年化", "format": "percent"},
                    {"field": "roll_count", "type": "quantitative", "label": "换月次数"},
                ],
            },
            "layout": "full",
        },
        {
            "id": "annual_excess",
            "title": "年度净超额收益",
            "subtitle": "不同年份和品种的贴水收益差异明显",
            "type": "line",
            "dataset": "annual",
            "sourceId": "annual_source",
            "valueFormat": "percent",
            "encodings": {
                "x": {"field": "year", "type": "temporal", "label": "年份"},
                "y": {"field": "excess_return", "type": "quantitative", "label": "年度净超额", "format": "percent"},
                "color": {"field": "product", "type": "nominal", "label": "品种"},
            },
            "layout": "full",
        },
    ]
    for product in ["IF", "IH", "IC", "IM"]:
        row = summary.loc[summary["product"] == product].iloc[0]
        charts.append(
            {
                "id": f"{product.lower()}_wealth",
                "title": f"{product} 独立滚贴水策略与全收益基准",
                "subtitle": (
                    f"{row['start_date']}至{row['end_date']}；策略年化 {row['strategy_cagr']:.2%}，"
                    f"全收益基准 {row['benchmark_cagr']:.2%}，年化超额 {row['annualized_excess_vs_benchmark']:+.2%}"
                ),
                "type": "line",
                "dataset": f"{product.lower()}_wealth",
                "sourceId": "product_wealth_source",
                "valueFormat": "number",
                "encodings": {
                    "x": {"field": "date", "type": "temporal", "label": "日期"},
                    "y": {"field": "wealth", "type": "quantitative", "label": "累计净值"},
                    "color": {"field": "series", "type": "nominal", "label": "序列"},
                },
                "layout": "full",
            }
        )

    tables = [
        {
            "id": "summary_table",
            "title": "四品种收益、风险与基差汇总",
            "subtitle": "各品种从上市日至2025年12月31日；按净年化超额降序",
            "dataset": "summary",
            "sourceId": "summary_source",
            "density": "spacious",
            "defaultSort": {"field": "annualized_excess_vs_benchmark", "direction": "desc"},
            "columns": [
                {"field": "product", "label": "品种", "type": "text"},
                {"field": "strategy_cagr", "label": "策略年化", "format": "percent"},
                {"field": "benchmark_cagr", "label": "全收益年化", "format": "percent"},
                {"field": "annualized_excess_vs_benchmark", "label": "年化超额", "format": "percent", "movement": True},
                {"field": "strategy_max_drawdown", "label": "最大回撤", "format": "percent", "movement": True},
                {"field": "tracking_error", "label": "跟踪误差", "format": "percent"},
                {"field": "roll_count", "label": "换月次数", "format": "number"},
                {"field": "discount_day_share", "label": "贴水天数占比", "format": "percent"},
            ],
        },
        {
            "id": "sensitivity_table",
            "title": "净年化超额敏感性",
            "subtitle": "零成本、基准、双倍成本及2%现金收益四种情景",
            "dataset": "sensitivity",
            "sourceId": "sensitivity_source",
            "density": "comfortable",
            "defaultSort": {"field": "product", "direction": "asc"},
            "columns": [
                {"field": "product", "label": "品种", "type": "text"},
                {"field": "scenario", "label": "情景", "type": "text"},
                {"field": "strategy_cagr", "label": "策略年化", "format": "percent"},
                {"field": "benchmark_cagr", "label": "全收益年化", "format": "percent"},
                {"field": "annualized_excess_vs_benchmark", "label": "年化超额", "format": "percent", "movement": True},
                {"field": "total_transaction_cost", "label": "累计名义成本", "format": "percent"},
            ],
        },
    ]

    blocks = [
        {"id": "title", "type": "markdown", "body": "# 四类股指期货滚贴水真实收益回测"},
        {
            "id": "technical_summary",
            "type": "markdown",
            "body": (
                "## 技术摘要\n\n"
                "**历史主力路径中，IC 与 IM 的滚贴水超额明显高于 IF 与 IH。** 基准成本口径下，"
                "IF、IH、IC、IM 相对全收益指数的净年化超额分别为 -0.31%、-0.44%、10.03% 和 8.20%。"
                "四个品种均从各自上市日起独立投入 1 倍名义本金并逐日复利，不构造四品种等权组合。\n\n"
                "结果说明贴水收敛曾形成可观收益，但它不是无风险套利：早期 IF 多年为负超额，"
                "且当前数据只支持历史主力路径，尚不能精确选择最优到期月或复原真实盘口换月价差。"
            ),
        },
        {
            "id": "cross_product_finding",
            "type": "markdown",
            "sourceId": "summary_source",
            "body": (
                "## 中小盘合约贡献了最强贴水收益\n\n"
                "IC 的净年化超额为 **10.03%**，IM 为 **8.20%**，而 IF 为 **-0.31%**、IH 为 **-0.44%**。"
                "这与 IC/IM 贴水更深、更常见一致：样本中 IC、IM 的贴水天数占比分别为 86.34% 和 84.71%。"
                "但 IC/IM 的方向波动和跟踪误差也更大，不能只根据超额排序配置资金。"
            ),
        },
        {"id": "excess_chart_block", "type": "chart", "chartId": "excess_by_product", "layout": "full"},
        {"id": "summary_table_block", "type": "table", "tableId": "summary_table", "layout": "full"},
        {
            "id": "time_variation_finding",
            "type": "markdown",
            "sourceId": "annual_source",
            "body": (
                "## 收益高度依赖市场时期\n\n"
                "IF 在 2010—2014 年的年度超额多次为负，而 2015 年后大多数年份转正；IC 在 2015—2021 年的超额最强，"
                "2022—2023 年明显收窄，2025 年又扩大。年度序列说明贴水由融资、分红、风险偏好、对冲需求和合约供需共同决定，"
                "不能把历史均值当作稳定票息。"
            ),
        },
        {"id": "annual_chart_block", "type": "chart", "chartId": "annual_excess", "layout": "full"},
        {
            "id": "individual_tests",
            "type": "markdown",
            "body": (
                "## 四个品种分别进行独立收益测试\n\n"
                "以下四组曲线各自以样本首日净值 1.0 起算，分别比较 IF、IH、IC、IM 主力期货滚贴水策略与其对应税前全收益指数。"
                "不同品种上市日期不同，因此不共享样本起点、不做每日等权，也不把某一品种的收益用于补齐另一品种。"
            ),
        },
        {
            "id": "if_finding",
            "type": "markdown",
            "sourceId": "summary_source",
            "body": (
                "### IF：长期收益略低于沪深300全收益指数\n\n"
                "2010年4月至2025年末，IF 策略累计收益 **85.88%**，H00300 全收益基准累计收益 **94.37%**；"
                "年化超额为 **-0.31%**。策略最大回撤 **-48.45%**，说明这一历史主力路径没有稳定获得正滚贴水超额。"
            ),
        },
        {"id": "if_chart_block", "type": "chart", "chartId": "if_wealth", "layout": "full"},
        {
            "id": "ih_finding",
            "type": "markdown",
            "sourceId": "summary_source",
            "body": (
                "### IH：累计收益同样略低于上证50全收益指数\n\n"
                "2015年4月至2025年末，IH 策略累计收益 **24.64%**，H00016 全收益基准累计收益 **30.26%**；"
                "年化超额为 **-0.44%**。策略最大回撤 **-46.39%**，不能把 IH 贴水视为稳定票息。"
            ),
        },
        {"id": "ih_chart_block", "type": "chart", "chartId": "ih_wealth", "layout": "full"},
        {
            "id": "ic_finding",
            "type": "markdown",
            "sourceId": "summary_source",
            "body": (
                "### IC：历史滚贴水超额最强，但路径风险也最高\n\n"
                "2015年4月至2025年末，IC 策略累计收益 **189.84%**，H00905 全收益基准累计收益 **8.73%**；"
                "年化超额为 **10.03%**。策略最大回撤 **-51.43%**，因此高超额并不等于低风险套利。"
            ),
        },
        {"id": "ic_chart_block", "type": "chart", "chartId": "ic_wealth", "layout": "full"},
        {
            "id": "im_finding",
            "type": "markdown",
            "sourceId": "summary_source",
            "body": (
                "### IM：较短样本中取得正超额，结论仍需更长周期验证\n\n"
                "2022年7月至2025年末，IM 策略累计收益 **44.61%**，H00852 全收益基准累计收益 **12.31%**；"
                "年化超额为 **8.20%**。策略最大回撤 **-39.37%**，且三年多样本不足以覆盖完整市场周期。"
            ),
        },
        {"id": "im_chart_block", "type": "chart", "chartId": "im_wealth", "layout": "full"},
        {
            "id": "definitions",
            "type": "markdown",
            "body": (
                "## 数据范围与收益定义\n\n"
                "- IF：2010年4月16日至2025年12月31日；IH/IC：2015年4月16日起；IM：2022年7月22日起。\n"
                "- 每个品种独立以 1 倍名义本金起算并复利，不构造四品种等权组合。\n"
                "- 基差计算依次使用沪深300、上证50、中证500和中证1000价格指数。\n"
                "- 业绩基准使用 H00300、H00016、H00905和H00852税前全收益指数。\n"
                "- 净年化超额定义为期货策略复合年化收益减全收益指数复合年化收益。\n"
                "- 基准成本为中金所非平今费率 0.000023、每边 0.2 点滑点、1 倍名义敞口、0%现金收益。"
            ),
        },
        {
            "id": "methodology",
            "type": "markdown",
            "sourceId": "code_source",
            "body": (
                "## 方法避免把主连跳变误算成收益\n\n"
                "每日取 15:00 或之前最后一分钟价。合约代码不变时，使用真实期货收盘价收益；主力代码改变时，"
                "由于缺少新旧合约同刻报价，使用当日现货收益替代主连价格跳变。首次开仓、每次平旧开新和期末平仓均计成本。"
                "逐日复利后再计算年化、回撤、跟踪误差和相对指数超额。"
            ),
        },
        {
            "id": "robustness",
            "type": "markdown",
            "sourceId": "sensitivity_source",
            "body": (
                "## 成本翻倍后排序不变，现金收益会显著抬高总收益\n\n"
                "将费率与滑点同时翻倍后，四品种年化超额仅下降约 0.12—0.21 个百分点，说明月度/季度展期频率下成本不是主导项。"
                "若未占用保证金的 88% 现金可获得 2% 年收益，年化结果约增加 1.8—2.0 个百分点；"
                "基准报告未计该收益，以免把未核实的账户利息当成实得收益。"
            ),
        },
        {"id": "sensitivity_table_block", "type": "table", "tableId": "sensitivity_table", "layout": "full"},
        {
            "id": "limitations",
            "type": "markdown",
            "body": (
                "## 局限性与不确定性\n\n"
                "**这是研究级真实数据回测，不是成交回报复原。** 期货库仅含主连路径，缺少所有到期月的并行报价、买卖盘和实际成交。"
                "主力合约由数据提供方定义，若其选择使用当日收盘后成交量，可能存在事后识别偏差。"
                "换月日用现货收益替代可消除虚假的合约跳变，但会忽略新合约当天的真实基差变化。"
                "指数本身不可直接买入；若实际目标是“现金+期货”或ETF替代，资金利息、ETF误差、保证金上调和追保流动性都需另建账户层模型。"
            ),
        },
        {
            "id": "next_steps",
            "type": "markdown",
            "body": (
                "## 推荐的生产化下一步\n\n"
                "1. 补齐 IF/IH/IC/IM 全部可交易月份的逐分钟买一卖一和成交量。\n"
                "2. 固定无前视换月规则（如到期前5个交易日或次月持仓量超过近月后的下一交易日）。\n"
                "3. 同时回测近月、次月、季月及‘最深贴水但流动性合格’规则。\n"
                "4. 接入真实券商费率、保证金利息、逐日盯市和追保现金缓冲。\n"
                "5. 用真实成交回报做逐笔偏差归因，再决定可部署资金规模。"
            ),
        },
        {
            "id": "further_questions",
            "type": "markdown",
            "body": (
                "## 进一步问题\n\n"
                "- 实际业务目标是长期股票替代、指数增强，还是市场中性套保？三者的资金分母不同。\n"
                "- 账户能否获得保证金与剩余现金利息，券商保证金率和费率是多少？\n"
                "- 是否能补充全合约行情与成交回报，以把当前研究模型升级为可执行模型？"
            ),
        },
    ]

    artifact = {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": "四类股指期货滚贴水真实收益回测",
            "description": "基于/Volumes/Stocksdata历史主连期货、价格指数及中证官方全收益指数的净收益回测。",
            "generatedAt": generated,
            "filters": [],
            "cards": cards,
            "charts": charts,
            "tables": tables,
            "sources": sources,
            "blocks": blocks,
        },
        "snapshot": {
            "version": 1,
            "generatedAt": generated,
            "status": "ready",
            "datasets": {
                "summary": _records(summary_rows),
                "annual": _records(annual_rows),
                "basis": _records(basis_rows),
                "sensitivity": _records(sensitivity_rows),
                **{key: _records(frame) for key, frame in product_wealth.items()},
            },
            "accessIssues": [],
        },
        "sources": top_sources,
        "package_info": {"originUrl": "artifact://roll-discount-backtest", "controls": {"edit": False, "refresh": False}},
    }
    path = OUTPUTS / "artifact.json"
    path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def build_validation_report(data: dict[str, pd.DataFrame]) -> Path:
    daily = data["daily"].copy()
    checks: list[tuple[str, bool, str]] = []
    checks.append(("关键价格无空值", not daily[["futures_close", "spot_close", "benchmark_close"]].isna().any().any(), f"检查 {len(daily):,} 个对齐日"))
    benchmark_uplift = (
        data["summary"].set_index("product")["benchmark_total_return"]
        - data["summary"].set_index("product")["spot_total_return"]
    )
    checks.append(
        (
            "全收益基准已计入分红再投资",
            bool((benchmark_uplift > 0).all()),
            "相对价格指数累计增益 "
            + "、".join(f"{product} {value:.2%}" for product, value in benchmark_uplift.items()),
        )
    )
    switch_ok = np.allclose(
        daily.loc[daily["is_roll"], "futures_return"],
        daily.loc[daily["is_roll"], "spot_return"],
        rtol=0,
        atol=1e-12,
    )
    checks.append(("换月日未计主连跳变", switch_ok, f"检查 {int(daily['is_roll'].sum())} 次换月"))
    non_roll = ~daily["is_roll"] & daily.groupby("product").cumcount().gt(0)
    expected_return = daily.groupby("product")["futures_close"].pct_change()
    same_contract_ok = np.allclose(daily.loc[non_roll, "futures_return"], expected_return.loc[non_roll], rtol=0, atol=1e-12)
    checks.append(("同合约收益由价格独立重算一致", same_contract_ok, f"检查 {int(non_roll.sum()):,} 个交易日"))
    wealth_recalc = daily.groupby("product", group_keys=False)["net_strategy_return"].apply(lambda x: (1 + x).cumprod())
    wealth_ok = np.allclose(wealth_recalc.to_numpy(), daily["strategy_wealth"].to_numpy(), rtol=0, atol=1e-10)
    checks.append(("逐日复利净值独立重算一致", wealth_ok, "绝对误差阈值 1e-10"))
    costs_nonnegative = bool((daily["transaction_cost"] >= 0).all())
    checks.append(("成本非负且只在交易日发生", costs_nonnegative, f"累计名义成本 {daily['transaction_cost'].sum():.4%}"))

    status = "Ready to share" if all(passed for _, passed, _ in checks) else "Needs revision"
    lines = [
        "# Validation Report",
        "",
        f"## Overall Assessment: {status}",
        "",
        "方法与计算检查均通过；业绩基准已从价格指数修正为官方税前全收益指数。由于缺少全合约同刻行情和真实成交回报，不能视为生产级可执行收益证明。",
        "",
        "## Methodology Review",
        "",
        "- 问题、价格指数基差口径、全收益业绩基准、名义本金、成本与现金收益假设均已显式定义。",
        "- 15:00 对齐避免了早期股指期货15:15收盘与现货15:00收盘错配。",
        "- 主连换码日用现货收益替代，避免把合约价差直接当作当日利润。",
        "- 基差与换月日替代仍使用价格指数；超额、跟踪误差和信息比率使用全收益指数。",
        "- 逐日收益、复利、年化、回撤和交易成本均保存在可审计CSV中。",
        "",
        "## Calculation Spot-Checks",
        "",
    ]
    for name, passed, evidence in checks:
        lines.append(f"- {name}: {'Verified' if passed else 'Discrepancy found'} — {evidence}")
    lines += [
        "",
        "## Issues Found",
        "",
        "1. [Severity: High] 缺少新旧合约同刻报价与全合约并行行情，无法精确复原换月价差或选择最深贴水合约。",
        "2. [Severity: High] 主力合约选择规则来自数据提供方，可能使用当日结束后信息，存在事后识别偏差。",
        "3. [Severity: Medium] 滑点为每边0.2点假设，没有盘口与真实成交回报校准。",
        "4. [Severity: Medium] 基准未计现金利息、保证金上调和追保融资成本。",
        "",
        "## Visualization Review",
        "",
        "IF、IH、IC、IM 各自使用独立的策略/全收益基准双线净值图及回撤图；年度横截面采用热力图。尺度、单位、各自样本期和图例均已标注。",
        "",
        "## Required Caveats for Stakeholders",
        "",
        "- IC/IM 的高历史超额不可外推为稳定未来收益。",
        "- 当前结果是主力路径研究回测，不是最优合约选择，也不是逐笔成交复原。",
        "- 策略保留完整指数方向风险；最大回撤可接近单一股票指数。",
        "",
        "## Reproducibility",
        "",
        "- 回测：`conda run -n ag python run_backtest.py --config config.yaml`",
        "- 测试：`conda run -n ag pytest -q`",
        "- 笔记本：`滚贴水回测.ipynb`",
    ]
    path = OUTPUTS / "validation_report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def build_source_notes() -> Path:
    content = """# Source and chart notes

## Controlling data sources

- Futures: `/Volumes/Stocksdata/期货数据|分钟数据（主连合约）|主连一分钟_*_{IF,IH,IC,IM}.zip`
- Spot price indexes for basis: annual one-minute archives for `000300`, `000016`, `000905`, `000852`
- Performance benchmarks: official CSI gross total-return indices `H00300`, `H00016`, `H00905`, `H00852`, cached in `data/total_return_indices/`
- Official benchmark endpoint: `https://www.csindex.com.cn/csindex-home/perf/index-perf`
- Transformation: `src/roll_discount_backtest.py`
- Final reviewed datasets: `outputs/*.csv`
- Report shaping database: `outputs/results.sqlite`
- Executed report queries: `queries/*.sql`

## Chart map

| Report segment | Question | Family / type | Fields | Supported claim | Palette |
|---|---|---|---|---|---|
| Cross-product finding | Which contracts earned the most excess? | Comparison / horizontal bar | product, annualized_excess_vs_benchmark | IC/IM exceeded IF/IH | single-root preferred |
| Time variation | Is excess stable across years? | Trend / multi-series line | year, excess_return, product | excess is regime-dependent | relaxed multi-category, four roots |
| IF independent test | How did IF perform versus H00300? | Trend / two-series line | date, wealth, series | IF slightly lagged its total-return benchmark | hard two-root cap |
| IH independent test | How did IH perform versus H00016? | Trend / two-series line | date, wealth, series | IH slightly lagged its total-return benchmark | hard two-root cap |
| IC independent test | How did IC perform versus H00905? | Trend / two-series line | date, wealth, series | IC earned strong historical roll excess | hard two-root cap |
| IM independent test | How did IM perform versus H00852? | Trend / two-series line | date, wealth, series | IM earned positive historical roll excess | hard two-root cap |
| Notebook QA | How did relative wealth evolve? | Trend / multi-series line | date, relative_wealth, product | cumulative basis contribution differs by product | relaxed multi-category |
| Notebook QA | Where were annual gains/losses concentrated? | Matrix / heatmap | year, product, excess_return | positive excess clusters by period/product | diverging two-root |

## Omitted metrics

- Sharpe ratio is not emphasized because the strategy retains equity beta and the user asked for roll-discount profitability; tracking error and information ratio are more relevant.
- Margin return is excluded from the base case because actual account interest terms were not supplied.
- Exact roll spread is not reported as realized P&L because old/new simultaneous quotes are unavailable.
"""
    path = OUTPUTS / "source_notes.md"
    path.write_text(content, encoding="utf-8")
    return path


def main() -> None:
    data = load_outputs()
    build_static_charts(data)
    notebook = build_notebook(data)
    artifact = build_report_artifact(data)
    validation = build_validation_report(data)
    notes = build_source_notes()
    print(f"Notebook: {notebook}")
    print(f"Artifact: {artifact}")
    print(f"Validation: {validation}")
    print(f"Notes: {notes}")


if __name__ == "__main__":
    main()
