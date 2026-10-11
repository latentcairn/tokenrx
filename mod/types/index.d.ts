export type Limit = { kind: string; percentUsed: number; resetsAt?: string }

/** 마지막으로 잰 값: 대화 맥락 크기, 한도 창들, 모델 */
export type Reading = { tokens?: number; window: number; limits: Limit[]; model: string }

declare module 'claude-code' {
  interface PluginState {
    tokenrx: { reading: Reading | null; lastReplyAt: number | null; now: number }
  }
}
