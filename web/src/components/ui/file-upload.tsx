"use client";

import { useState } from "react";
import { motion } from "motion/react";
import { IconFileAnalytics, IconUpload, IconX } from "@tabler/icons-react";
import { useDropzone } from "react-dropzone";
import { cn } from "../../lib/utils";

export function FileUpload({ onChange }: { onChange: (file: File) => void | Promise<void> }) {
  const [file, setFile] = useState<File | null>(null);
  const acceptFile = async (next: File) => { setFile(next); await onChange(next); };
  const { getRootProps, getInputProps, isDragActive, fileRejections, open } = useDropzone({
    multiple: false,
    accept: { "text/csv": [".csv"], "application/json": [".json"] },
    onDropAccepted: (files) => void acceptFile(files[0]),
  });
  return (
    <div {...getRootProps()} className={cn("upload-zone", isDragActive && "upload-zone-active", fileRejections.length > 0 && "upload-zone-error")}>
      <input {...getInputProps()} />
      <div className="upload-grid" aria-hidden="true" />
      {file ? (
        <motion.div layoutId="uploaded-file" className="uploaded-file" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}>
          <IconFileAnalytics size={34} />
          <div><strong>{file.name}</strong><span>{(file.size / 1024).toFixed(1)} KB · 已载入本地分析</span></div>
          <button type="button" onClick={(event) => { event.stopPropagation(); setFile(null); }} aria-label="移除文件"><IconX size={18} /></button>
        </motion.div>
      ) : (
        <button type="button" className="upload-prompt" onClick={open}>
          <motion.span className="upload-symbol" animate={isDragActive ? { y: -8, scale: 1.05 } : { y: 0, scale: 1 }}><IconUpload size={28} /></motion.span>
          <strong>{isDragActive ? "松开以载入K线" : "上传K线数据"}</strong>
          <span>拖入 CSV 或 JSON，字段需包含 time、open、high、low、close、volume</span>
        </button>
      )}
      {fileRejections.length > 0 && <p className="upload-error">文件格式不支持，请选择 CSV 或 JSON。</p>}
    </div>
  );
}
