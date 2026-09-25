/** Node box dimensions, shared by the layout (which has to know how tall a node
 *  will be before drawing it) and the renderer. */
export const NODE_W = 176
export const HEAD = 22
export const SUB = 16
export const LIVE = 18
export const ROW = 18
export const PAD = 8

export const nodeHeight = (rows: number, hasLive: boolean) =>
  HEAD + SUB + (hasLive ? LIVE : 0) + rows * ROW + PAD
