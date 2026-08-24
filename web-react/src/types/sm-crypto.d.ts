declare module 'sm-crypto' {
  export function sm3(msg: string): string;
  export function sm2Encrypt(msg: string, publicKey: string, cipherMode?: number): string;
  export function sm2Decrypt(enc: string, privateKey: string, cipherMode?: number): string;
  export function sm4Encrypt(msg: string, key: string, options?: any): string;
  export function sm4Decrypt(enc: string, key: string, options?: any): string;
}
