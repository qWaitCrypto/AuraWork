import React, { useRef, useCallback } from "react";
import type { MutableRefObject } from "react";
import type { BrowserFrameMetadata } from "../lib/ws";

function browserModifiers(e: { altKey?: boolean; ctrlKey?: boolean; metaKey?: boolean; shiftKey?: boolean }): number {
  return (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0);
}

function browserVKeyCode(key: string): number | null {
  switch (key) {
    case "Backspace":
      return 8;
    case "Tab":
      return 9;
    case "Enter":
      return 13;
    case "Escape":
      return 27;
    case "ArrowLeft":
      return 37;
    case "ArrowUp":
      return 38;
    case "ArrowRight":
      return 39;
    case "ArrowDown":
      return 40;
    case "Delete":
      return 46;
    default:
      return null;
  }
}

function browserButtonFromMouseButton(btn: number): "left" | "middle" | "right" {
  if (btn === 1) return "middle";
  if (btn === 2) return "right";
  return "left";
}

function browserButtonFromButtonsMask(buttons: number | undefined): "none" | "left" | "middle" | "right" {
  const value = typeof buttons === "number" ? buttons : 0;
  if (value & 1) return "left";
  if (value & 4) return "middle";
  if (value & 2) return "right";
  return "none";
}

type BrowserMousePayload = {
  type: "input_mouse";
  eventType: "mouseMoved" | "mousePressed" | "mouseReleased" | "mouseWheel";
  x: number;
  y: number;
  modifiers: number;
  button?: "none" | "left" | "middle" | "right";
  clickCount?: number;
  buttons?: number;
  deltaX?: number;
  deltaY?: number;
};

type BrowserKeyboardPayload = {
  type: "input_keyboard";
  eventType: "keyDown" | "keyUp" | "char";
  key?: string;
  code?: string;
  modifiers?: number;
  text?: string;
  unmodifiedText?: string;
  windowsVirtualKeyCode?: number;
  nativeVirtualKeyCode?: number;
};

type BrowserInputPayload = BrowserMousePayload | BrowserKeyboardPayload;

export interface UseBrowserInputOpts {
  browserControl: boolean;
  browserControlFocused: boolean;
  setBrowserControl: (v: boolean) => void;
  setBrowserControlFocused: (v: boolean) => void;
  browserWsRef: MutableRefObject<WebSocket | null>;
  browserImgRef: MutableRefObject<HTMLImageElement | null>;
  browserStageRef: MutableRefObject<HTMLDivElement | null>;
  browserFrameRef: MutableRefObject<{ data: string; metadata: BrowserFrameMetadata | null; ts: number }>;
  browserMouseMoveRef: MutableRefObject<{ x: number; y: number; modifiers: number; buttons?: number } | null>;
  browserMouseMoveRafRef: MutableRefObject<number | null>;
}

export function useBrowserInput(opts: UseBrowserInputOpts) {
  const {
    browserControl,
    browserControlFocused,
    setBrowserControl,
    setBrowserControlFocused,
    browserWsRef,
    browserImgRef,
    browserStageRef,
    browserFrameRef,
    browserMouseMoveRef,
    browserMouseMoveRafRef,
  } = opts;

  const browserPointerDownRef = useRef<{
    pointerId: number;
    button: "left" | "middle" | "right";
    clickCount: number;
  } | null>(null);

  const sendWsMsg = useCallback((payload: BrowserInputPayload) => {
    const ws = browserWsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    try {
      ws.send(JSON.stringify(payload));
    } catch {
    }
  }, [browserWsRef]);

  const devicePointFromClient = useCallback((clientX: number, clientY: number): { x: number; y: number } | null => {
    const img = browserImgRef.current;
    if (!img) return null;

    const meta = browserFrameRef.current.metadata;
    const deviceWidth = Number(meta?.deviceWidth || img.naturalWidth || 0);
    const deviceHeight = Number(meta?.deviceHeight || img.naturalHeight || 0);
    if (!deviceWidth || !deviceHeight) return null;

    const rect = img.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return null;

    const relX = (clientX - rect.left) / rect.width;
    const relY = (clientY - rect.top) / rect.height;
    if (!Number.isFinite(relX) || !Number.isFinite(relY)) return null;
    if (relX < 0 || relY < 0 || relX > 1 || relY > 1) return null;

    const x = Math.round(relX * deviceWidth);
    const y = Math.round(relY * deviceHeight);
    if (x < 0 || y < 0 || x > deviceWidth || y > deviceHeight) return null;

    return { x, y };
  }, [browserImgRef, browserFrameRef]);

  const focusBrowserControl = useCallback(() => {
    const el = browserStageRef.current;
    if (!el) return;
    try {
      el.focus();
      setBrowserControlFocused(true);
    } catch {
    }
  }, [browserStageRef, setBrowserControlFocused]);

  const onPointerMove = useCallback((e: React.PointerEvent) => {
    if (!browserControl) return;
    e.preventDefault();
    e.stopPropagation();

    const point = devicePointFromClient(e.clientX, e.clientY);
    if (!point) return;

    browserMouseMoveRef.current = { x: point.x, y: point.y, modifiers: browserModifiers(e), buttons: e.buttons };
    if (browserMouseMoveRafRef.current != null) return;

    browserMouseMoveRafRef.current = requestAnimationFrame(() => {
      browserMouseMoveRafRef.current = null;
      const cur = browserMouseMoveRef.current;
      if (!cur) return;

      const down = browserPointerDownRef.current;
      sendWsMsg({
        type: "input_mouse",
        eventType: "mouseMoved",
        x: cur.x,
        y: cur.y,
        modifiers: cur.modifiers,
        button: down?.button ?? browserButtonFromButtonsMask(cur.buttons),
        clickCount: down?.clickCount ?? 0,
        buttons: cur.buttons,
      });
    });
  }, [browserControl, devicePointFromClient, sendWsMsg, browserMouseMoveRef, browserMouseMoveRafRef]);

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    if (!browserControl) return;
    focusBrowserControl();
    try {
      browserStageRef.current?.setPointerCapture(e.pointerId);
    } catch {
    }

    const point = devicePointFromClient(e.clientX, e.clientY);
    if (!point) return;

    const modifiers = browserModifiers(e);
    const button = browserButtonFromMouseButton(e.button);
    browserPointerDownRef.current = { pointerId: e.pointerId, button, clickCount: 1 };

    e.preventDefault();
    e.stopPropagation();

    sendWsMsg({ type: "input_mouse", eventType: "mouseMoved", x: point.x, y: point.y, modifiers });
    sendWsMsg({
      type: "input_mouse",
      eventType: "mousePressed",
      x: point.x,
      y: point.y,
      button,
      clickCount: 1,
      modifiers,
      buttons: e.buttons,
    });
  }, [browserControl, focusBrowserControl, devicePointFromClient, sendWsMsg, browserStageRef]);

  const onPointerUp = useCallback((e: React.PointerEvent) => {
    if (!browserControl) return;
    focusBrowserControl();

    const point = devicePointFromClient(e.clientX, e.clientY);
    if (!point) return;

    const modifiers = browserModifiers(e);
    const down = browserPointerDownRef.current;
    const button = down?.button ?? browserButtonFromMouseButton(e.button);

    e.preventDefault();
    e.stopPropagation();

    sendWsMsg({ type: "input_mouse", eventType: "mouseMoved", x: point.x, y: point.y, modifiers });
    sendWsMsg({
      type: "input_mouse",
      eventType: "mouseReleased",
      x: point.x,
      y: point.y,
      button,
      clickCount: down?.clickCount ?? 1,
      modifiers,
      buttons: e.buttons,
    });

    browserPointerDownRef.current = null;
    try {
      browserStageRef.current?.releasePointerCapture(e.pointerId);
    } catch {
    }
  }, [browserControl, focusBrowserControl, devicePointFromClient, sendWsMsg, browserStageRef]);

  const onWheel = useCallback((e: React.WheelEvent) => {
    if (!browserControl) return;
    focusBrowserControl();

    const point = devicePointFromClient(e.clientX, e.clientY);
    if (!point) return;

    e.preventDefault();
    const modifiers = browserModifiers(e);
    sendWsMsg({
      type: "input_mouse",
      eventType: "mouseWheel",
      x: point.x,
      y: point.y,
      deltaX: e.deltaX || 0,
      deltaY: e.deltaY || 0,
      modifiers,
      button: "none",
      clickCount: 0,
    });
  }, [browserControl, focusBrowserControl, devicePointFromClient, sendWsMsg]);

  const onKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (!browserControl || !browserControlFocused) return;

    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      setBrowserControl(false);
      setBrowserControlFocused(false);
      return;
    }

    e.preventDefault();
    e.stopPropagation();

    const modifiers = browserModifiers(e);
    const key = String(e.key || "");
    const code = String(e.code || "");
    const virtualKey = browserVKeyCode(key);
    const isPrintable = key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey;
    const text = isPrintable ? key : null;

    sendWsMsg({
      type: "input_keyboard",
      eventType: "keyDown",
      key,
      code,
      modifiers,
      text: text ?? undefined,
      unmodifiedText: text ?? undefined,
      windowsVirtualKeyCode: virtualKey ?? undefined,
      nativeVirtualKeyCode: virtualKey ?? undefined,
    });

    if (isPrintable) {
      sendWsMsg({ type: "input_keyboard", eventType: "char", text: key, key, code, modifiers });
    }
  }, [browserControl, browserControlFocused, setBrowserControl, setBrowserControlFocused, sendWsMsg]);

  const onKeyUp = useCallback((e: React.KeyboardEvent) => {
    if (!browserControl || !browserControlFocused) return;

    e.preventDefault();
    e.stopPropagation();

    const modifiers = browserModifiers(e);
    const key = String(e.key || "");
    const code = String(e.code || "");
    const virtualKey = browserVKeyCode(key);

    sendWsMsg({
      type: "input_keyboard",
      eventType: "keyUp",
      key,
      code,
      modifiers,
      windowsVirtualKeyCode: virtualKey ?? undefined,
      nativeVirtualKeyCode: virtualKey ?? undefined,
    });
  }, [browserControl, browserControlFocused, sendWsMsg]);

  return {
    onPointerMove,
    onPointerDown,
    onPointerUp,
    onWheel,
    onKeyDown,
    onKeyUp,
    focusBrowserControl,
  };
}
