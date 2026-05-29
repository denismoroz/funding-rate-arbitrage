export function ErrorMsg({ message }: { message: string }) {
  return (
    <p className="rounded border border-red-300 bg-red-50 p-3 text-sm text-red-700">
      {message}
    </p>
  );
}
