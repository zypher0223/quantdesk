import { useEffect, useState } from "react";
import { IconAlertTriangle, IconClockPlay } from "@tabler/icons-react";
import { useInstrumentSymbols } from "../hooks/use-instrument-symbols";
import { readValidationStudy, submitRun, validationStudyBody } from "../services/runs";

const INTERVALS = ["15m", "1h", "4h", "1d"];
const DEFAULT_SYMBOL = "BTCUSDT";

/**
 * Queue a parameter search plus walk-forward over one contract.
 *
 * The engine runs this on its own worker, so the form submits and stops: the
 * numbers arrive later in the run list, never here. Every field is checked
 * against the engine's own bounds before anything is sent, and the answer the
 * engine gives back — queued, or merged with an identical active task — is shown
 * as it came.
 */
export function ValidationSubmitForm({ onSubmitted }: { onSubmitted?: () => void }) {
  const symbols = useInstrumentSymbols();
  const [symbol, setSymbol] = useState(DEFAULT_SYMBOL);
  const [interval, setIntervalValue] = useState("1h");
  const [bars, setBars] = useState("2000");
  const [fastGrid, setFastGrid] = useState("5,9,20");
  const [slowGrid, setSlowGrid] = useState("21,50,100");
  const [walkForwardWindows, setWalkForwardWindows] = useState("4");
  const [allowDegraded, setAllowDegraded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    if (symbols.length === 0) return;
    setSymbol((current) => (symbols.includes(current) ? current : symbols[0]));
  }, [symbols]);

  const submit = async () => {
    const read = readValidationStudy({ symbol, interval, bars, fastGrid, slowGrid, walkForwardWindows, allowDegraded });
    if (!read.ok) {
      setNotice("");
      setError(read.error);
      return;
    }
    setBusy(true);
    try {
      const outcome = await submitRun(
        "validate",
        validationStudyBody(read.input),
        `参数搜索与滚动验证 ${read.input.symbol} ${read.input.interval}`,
      );
      setError("");
      setNotice(`已提交到结果中心（#${outcome.run.id}）${outcome.deduplicated ? " · 与执行中的相同任务合并，未重复执行" : ""}`);
      onSubmitted?.();
    } catch (reason) {
      setNotice("");
      setError(reason instanceof Error ? reason.message : "提交失败");
    } finally {
      setBusy(false);
    }
  };

  return <div className="study-submit">
    <div className="run-toolbar">
      <label>合约
        {symbols.length > 0
          ? <select value={symbol} onChange={(event) => setSymbol(event.target.value)}>
            {symbols.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
          : <input value={symbol} onChange={(event) => setSymbol(event.target.value)} placeholder={DEFAULT_SYMBOL} />}
      </label>
      <label>周期
        <select value={interval} onChange={(event) => setIntervalValue(event.target.value)}>
          {INTERVALS.map((item) => <option key={item} value={item}>{item}</option>)}
        </select>
      </label>
      <label>K 线根数
        <input type="number" min={30} max={5000} step={100} value={bars} onChange={(event) => setBars(event.target.value)} />
      </label>
      <label>快线周期
        <input value={fastGrid} onChange={(event) => setFastGrid(event.target.value)} placeholder="5,9,20" />
      </label>
      <label>慢线周期
        <input value={slowGrid} onChange={(event) => setSlowGrid(event.target.value)} placeholder="21,50,100" />
      </label>
      <label>滚动窗口
        <input type="number" min={1} max={12} step={1} value={walkForwardWindows} onChange={(event) => setWalkForwardWindows(event.target.value)} />
      </label>
      <label className="run-toggle">
        <input type="checkbox" checked={allowDegraded} onChange={(event) => setAllowDegraded(event.target.checked)} />
        允许降级
      </label>
      <button type="button" onClick={() => void submit()} disabled={busy}>
        <IconClockPlay size={14} />{busy ? "提交中…" : "提交"}
      </button>
    </div>

    <p className="backtest-hint">
      双均线规则的参数网格逐组在训练段搜索、在滚动窗口中验证；数据不完整时默认拒绝运行，勾选「允许降级」会带着缺口跑并在结果上标注。
    </p>
    {error && <p className="history-error" role="alert"><IconAlertTriangle size={15} />{error}</p>}
    {notice && <p className="study-submit-note" role="status">{notice}</p>}
  </div>;
}
