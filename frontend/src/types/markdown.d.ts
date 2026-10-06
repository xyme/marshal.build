// S15-04: documentation markdown is imported as a string (see next.config.ts).
declare module "*.md" {
  const content: string;
  export default content;
}
