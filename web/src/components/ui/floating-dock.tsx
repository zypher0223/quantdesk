"use client";

import type { ReactNode } from "react";
import { useRef, useState } from "react";
import { AnimatePresence, motion, useMotionValue, useSpring, useTransform, type MotionValue } from "motion/react";
import { IconLayoutNavbarCollapse, IconX } from "@tabler/icons-react";
import { cn } from "../../lib/utils";

export interface DockItem {
  title: string;
  icon: ReactNode;
  onSelect: () => void;
  active?: boolean;
}

export function FloatingDock({ items, className }: { items: DockItem[]; className?: string }) {
  return (
    <>
      <FloatingDockDesktop items={items} className={className} />
      <FloatingDockMobile items={items} className={className} />
    </>
  );
}

function FloatingDockMobile({ items, className }: { items: DockItem[]; className?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={cn("fixed bottom-5 right-5 z-50 md:hidden", className)}>
      <AnimatePresence>
        {open && (
          <motion.div initial={{ opacity: 0, y: 12, filter: "blur(8px)" }} animate={{ opacity: 1, y: 0, filter: "blur(0px)" }} exit={{ opacity: 0, y: 8 }} className="mb-3 flex flex-col items-end gap-2">
            {items.map((item) => (
              <button key={item.title} type="button" onClick={() => { item.onSelect(); setOpen(false); }} className={cn("dock-mobile-item", item.active && "dock-mobile-active")} aria-label={item.title}>
                <span>{item.title}</span>{item.icon}
              </button>
            ))}
          </motion.div>
        )}
      </AnimatePresence>
      <button type="button" onClick={() => setOpen((value) => !value)} className="dock-toggle" aria-label={open ? "关闭功能导航" : "打开功能导航"} aria-expanded={open}>
        {open ? <IconX size={20} /> : <IconLayoutNavbarCollapse size={20} />}
      </button>
    </div>
  );
}

function FloatingDockDesktop({ items, className }: { items: DockItem[]; className?: string }) {
  const mouseX = useMotionValue(Number.POSITIVE_INFINITY);
  return (
    <motion.nav onMouseMove={(event) => mouseX.set(event.pageX)} onMouseLeave={() => mouseX.set(Number.POSITIVE_INFINITY)} className={cn("dock-desktop", className)} aria-label="功能导航">
      {items.map((item) => <DockIcon key={item.title} item={item} mouseX={mouseX} />)}
    </motion.nav>
  );
}

function DockIcon({ item, mouseX }: { item: DockItem; mouseX: MotionValue<number> }) {
  const ref = useRef<HTMLButtonElement>(null);
  const distance = useTransform(mouseX, (value) => {
    const bounds = ref.current?.getBoundingClientRect() ?? { x: 0, width: 0 };
    return value - bounds.x - bounds.width / 2;
  });
  const size = useSpring(useTransform(distance, [-130, 0, 130], [42, 58, 42]), { mass: 0.12, stiffness: 190, damping: 16 });
  const [hovered, setHovered] = useState(false);
  return (
    <motion.button ref={ref} style={{ width: size, height: size }} type="button" className={cn("dock-icon", item.active && "dock-icon-active")} onClick={item.onSelect} onMouseEnter={() => setHovered(true)} onMouseLeave={() => setHovered(false)} onFocus={() => setHovered(true)} onBlur={() => setHovered(false)} aria-label={item.title}>
      <AnimatePresence>{hovered && <motion.span initial={{ opacity: 0, y: 4, filter: "blur(5px)" }} animate={{ opacity: 1, y: 0, filter: "blur(0px)" }} exit={{ opacity: 0, y: 3 }} className="dock-tooltip">{item.title}</motion.span>}</AnimatePresence>
      <span className="dock-icon-glyph">{item.icon}</span>
    </motion.button>
  );
}
