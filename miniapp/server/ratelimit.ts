/** Токен-бакет: кадры летят с камеры пачками, модель зрения — платная и медленная. */

export class TokenBucket {
  private tokens: number;
  private last: number;
  private readonly perSec: number;
  private readonly burst: number;
  private readonly clock: () => number;

  constructor(perSec: number, burst: number, clock: () => number = () => Date.now()) {
    if (perSec <= 0 || burst <= 0) throw new Error("бакет обязан быть положительным");
    this.perSec = perSec;
    this.burst = burst;
    this.clock = clock;
    this.tokens = burst;
    this.last = clock();
  }

  /** true — пропустить можно; false — кадр лишний, его молча выбрасываем (счётчик ведёт вызывающий). */
  allow(): boolean {
    const now = this.clock();
    this.tokens = Math.min(this.burst, this.tokens + ((now - this.last) / 1000) * this.perSec);
    this.last = now;
    if (this.tokens >= 1) {
      this.tokens -= 1;
      return true;
    }
    return false;
  }
}
