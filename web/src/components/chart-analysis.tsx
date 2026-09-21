import { useCallback, useMemo, useState } from "react";
import { useDropzone } from "react-dropzone";
import { IconAlertTriangle, IconCamera, IconInfoCircle, IconLoader2, IconPhoto, IconX } from "@tabler/icons-react";
import { analyzeChart, readFileAsDataUrl, type ChartAnalysis } from "../services/llm";
import { RISK_COPY, TIMEFRAMES, type Instrument } from "../data/market";

const MAX_BYTES = 8 * 1024 * 1024;
const ACCEPTED = { "image/png": [".png"], "image/jpeg": [".jpg", ".jpeg"], "image/webp": [".webp"] };

export function ChartAnalysisWorkspace({ instrument, timeframe }: { instrument: Instrument | null; timeframe: string }) {
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [dataUrl, setDataUrl] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [scope, setScope] = useState<string>(instrument?.venueSymbol ?? "");
  const [frame, setFrame] = useState<string>(timeframe);
  const [result, setResult] = useState<ChartAnalysis | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const onDrop = useCallback(async (accepted: File[]) => {
    const next = accepted[0];
    if (!next) return;
    setResult(null);
    if (next.size > MAX_BYTES) {
      setError(`图片 ${(next.size / 1024 / 1024).toFixed(1)} MB，超过 8 MB 限制。`);
      return;
    }
    setError(null);
    setFile(next);
    try {
      const url = await readFileAsDataUrl(next);
      setDataUrl(url);
      setPreview(url);
    } catch {
      setError("读取图片失败");
    }
  }, []);

  const { getRootProps, getInputProps, isDragActive, fileRejections, open } = useDropzone({
    multiple: false,
    accept: ACCEPTED,
    onDropAccepted: (files) => void onDrop(files),
  });

  const reset = () => {
    setFile(null);
    setPreview(null);
    setDataUrl(null);
    setResult(null);
    setError(null);
  };

  const submit = async () => {
    if (!dataUrl) return;
    setBusy(true);
    setError(null);
    try {
      const analysis = await analyzeChart({
        imageBase64: dataUrl,
        fileName: file?.name,
        symbol: scope || undefined,
        timeframe: frame || undefined,
        note: note || undefined,
      });
      setResult(analysis);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "识别失败");
    } finally {
      setBusy(false);
    }
  };

  const structured = result?.structured ?? null;
  const uncertainty = useMemo(() => {
    const fromModel = structured?.uncertain ?? [];
    // The engine's own limits, always shown next to the model's list.
    const engine = [
      "截图无法替代交易所实时行情：精确OHLC、成交量、资金费率、持仓量均由引擎另行取数",
      "截图中的周期与标的若与图表标注不一致，以你选择的下拉项为准",
    ];
    return [...fromModel, ...engine];
  }, [structured]);

  return (
    <div className="analysis-workspace">
      <header className="analysis-heading">
        <div>
          <h2>K线截图识别</h2>
          <p>用视觉模型读取截图里的结构信息。结果只是观察记录，不是交易指令；价格与指标仍以引擎实时数据为准。</p>
        </div>
        {instrument && <span className="analysis-scope">{instrument.displaySymbol} · {RISK_COPY[instrument.riskClass]}</span>}
      </header>

      <div className="analysis-layout">
        <section className="analysis-input">
          <div
            {...getRootProps()}
            className={`shot-zone ${isDragActive ? "shot-zone-active" : ""} ${fileRejections.length ? "shot-zone-error" : ""} ${preview ? "shot-zone-filled" : ""}`}
          >
            <input {...getInputProps()} />
            {preview ? (
              <>
                <img src={preview} alt="待识别截图预览" />
                <button type="button" className="shot-remove" onClick={(event) => { event.stopPropagation(); reset(); }} aria-label="移除截图">
                  <IconX size={16} />
                </button>
              </>
            ) : (
              <button type="button" className="shot-prompt" onClick={open}>
                <IconCamera size={26} />
                <strong>{isDragActive ? "松开以载入截图" : "上传K线截图"}</strong>
                <span>拖入或点击选择 PNG / JPEG / WebP，单张不超过 8 MB</span>
                <em>图片只发送到本机网关配置的模型，不会经过第三方中转</em>
              </button>
            )}
          </div>
          {fileRejections.length > 0 && <p className="shot-error">格式不支持，请选择 PNG / JPEG / WebP。</p>}

          <div className="analysis-fields">
            <label>
              标的（提供给模型的上下文）
              <select value={scope} onChange={(event) => setScope(event.target.value)}>
                <option value="">不指定</option>
                {instrument && (
                  <option value={instrument.venueSymbol}>
                    {instrument.displaySymbol}（当前 · {instrument.venueSymbol}）
                  </option>
                )}
              </select>
            </label>
            <label>
              周期
              <select value={frame} onChange={(event) => setFrame(event.target.value)}>
                {TIMEFRAMES.map((item) => <option key={item} value={item}>{item}</option>)}
              </select>
            </label>
            <label>
              补充说明（可选）
              <input value={note} onChange={(event) => setNote(event.target.value)} placeholder="例如：注意图中标注的缺口位置" maxLength={200} />
            </label>
          </div>

          <button type="button" className="analysis-run" onClick={submit} disabled={!dataUrl || busy}>
            {busy ? <IconLoader2 size={16} className="spin" /> : <IconPhoto size={16} />}
            {busy ? "识别中…" : "开始识别"}
          </button>
          {error && <p className="analysis-error" role="alert">{error}</p>}
        </section>

        <section className="analysis-output">
          {!result ? (
            <div className="backtest-empty">
              <strong>上传截图后开始识别</strong>
              <span>识别结果会保存到本地数据库的 chart_analyses 表，并记录所用 profile 与模型。</span>
            </div>
          ) : (
            <>
              <div className="analysis-meta">
                <span>profile {result.profile}</span>
                <span>model {result.model}</span>
                <span>{result.latencySeconds}s</span>
                {result.usage?.total_tokens != null && <span>{result.usage.total_tokens} tokens</span>}
                {structured?.confidence && <span className={`confidence confidence-${structured.confidence}`}>置信度 {structured.confidence}</span>}
              </div>

              {!structured && (
                <p className="analysis-error" role="alert">模型返回的不是 JSON，以下为原文，请人工判读。</p>
              )}

              <dl className="analysis-facts">
                <div>
                  <dt>图中标的</dt>
                  <dd>{structured?.instrument?.visible ? structured.instrument.value || "已标注但为空" : "图中未见标注"}</dd>
                </div>
                <div>
                  <dt>图中周期</dt>
                  <dd>{structured?.timeframe?.visible ? structured.timeframe.value || "已标注但为空" : "图中未见标注"}</dd>
                </div>
                <div><dt>趋势</dt><dd>{structured?.trend ?? "—"}</dd></div>
                <div><dt>可见指标</dt><dd>{structured?.indicators_visible?.join("、") || "—"}</dd></div>
              </dl>

              {structured?.structure && structured.structure.length > 0 && (
                <section className="analysis-block">
                  <h3>结构要点</h3>
                  <ul>{structured.structure.map((line, index) => <li key={index}>{line}</li>)}</ul>
                </section>
              )}

              {structured?.levels && structured.levels.length > 0 && (
                <section className="analysis-block">
                  <h3>价位</h3>
                  <div className="backtest-table-wrap">
                    <table>
                      <thead><tr><th>价位</th><th>类型</th><th>依据</th></tr></thead>
                      <tbody>
                        {structured.levels.map((level, index) => (
                          <tr key={index}><td>{level.price ?? "—"}</td><td>{level.kind ?? "—"}</td><td>{level.note ?? "—"}</td></tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </section>
              )}

              {structured?.patterns && structured.patterns.length > 0 && (
                <section className="analysis-block">
                  <h3>形态</h3>
                  <p>{structured.patterns.join("、")}</p>
                </section>
              )}

              <section className="analysis-block analysis-uncertain">
                <h3><IconAlertTriangle size={14} />无法确认 / 不可替代</h3>
                <ul>{uncertainty.map((line, index) => <li key={index}>{line}</li>)}</ul>
              </section>

              <p className="analysis-disclaimer"><IconInfoCircle size={14} />{result.disclaimer}</p>

              <details className="analysis-raw">
                <summary>模型原文</summary>
                <pre>{result.raw}</pre>
              </details>
            </>
          )}
        </section>
      </div>
    </div>
  );
}
