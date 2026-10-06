import Image from "next/image";
import { redirect } from "next/navigation";
import { auth } from "@/auth";
import SignInButton from "@/components/SignInButton";

export default async function LandingPage({
  searchParams,
}: {
  searchParams: Promise<{ callbackUrl?: string }>;
}) {
  const session = await auth();
  if (session?.user) redirect("/home");
  const { callbackUrl } = await searchParams;

  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-slate-950 text-slate-100">
      <div className="w-full max-w-md rounded-2xl border border-slate-800 bg-slate-900/60 p-10 text-center shadow-2xl">
        <Image
          src="/marshal-logo.png"
          alt="marshal"
          width={96}
          height={96}
          priority
          className="mx-auto mb-6 rounded-2xl"
        />
        <h1 className="text-2xl font-semibold tracking-tight">marshal.build</h1>
        <p className="mt-1 text-xs font-medium uppercase tracking-[0.25em] text-indigo-400">
          Build · Govern · Deploy
        </p>
        <p className="mt-3 text-sm leading-6 text-slate-400">
          Describe the AI agent you need in plain English. marshal turns it into
          a structured spec and deploys it to a governed AWS Enclave.
        </p>
        <SignInButton callbackUrl={callbackUrl} />
        <p className="mt-4 text-xs text-slate-400">
          Cognito sign-in (enterprise SSO federation available via configuration) · Alpha
        </p>
      </div>
    </div>
  );
}
