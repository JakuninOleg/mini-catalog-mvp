import Image from "next/image";

type Product = { id: number; title: string; image_url: string };

export const dynamic = "force-dynamic";

export default async function Catalog({
  searchParams,
}: {
  searchParams: Promise<{ before?: string }>;
}) {
  const { before } = await searchParams;
  const cursor =
    before && /^\d{1,18}$/.test(before) ? `?before_id=${before}` : "";
  let products: Product[] = [];
  let failed = false;
  try {
    const response = await fetch(
      `${process.env.BACKEND_URL ?? "http://127.0.0.1:8000"}/products${cursor}`,
      {
        cache: "no-store",
        signal: AbortSignal.timeout(8000),
      },
    );
    if (!response.ok) throw new Error("Catalog unavailable");
    products = (await response.json()) as Product[];
  } catch {
    failed = true;
  }

  return (
    <main className="mx-auto max-w-6xl px-5 pt-12 pb-[max(3rem,env(safe-area-inset-bottom))] sm:px-10 sm:pt-20">
      <header className="mb-8 flex items-end justify-between gap-4 border-b border-neutral-300 pb-6 sm:mb-10">
        <h1 className="text-4xl font-medium tracking-tight sm:text-5xl">
          Каталог
        </h1>
        {/* Full reload also refreshes the catalog when viewed inside Telegram. */}
        {/* eslint-disable-next-line @next/next/no-html-link-for-pages */}
        <a
          className="py-2 text-sm text-neutral-600 underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-4"
          href="/"
        >
          Обновить
        </a>
      </header>
      {failed ? (
        <section
          role="alert"
          className="border border-neutral-300 bg-white p-7 sm:p-10"
        >
          <h2 className="text-xl font-medium">Каталог временно недоступен</h2>
          <p className="mt-3 max-w-lg text-neutral-600">
            Не удалось загрузить товары. Обновите страницу через некоторое
            время.
          </p>
        </section>
      ) : products.length === 0 ? (
        <section className="border border-neutral-300 bg-white p-7 sm:p-10">
          <h2 className="text-xl font-medium">
            {cursor ? "Больше товаров нет" : "Пока нет товаров"}
          </h2>
          <p className="mt-3 max-w-lg leading-relaxed text-neutral-600">
            {cursor
              ? "Вернитесь к началу каталога, чтобы увидеть последние добавления."
              : "Товары появятся здесь, когда администратор добавит их через Telegram-бота."}
          </p>
        </section>
      ) : (
        <ul className="grid grid-cols-1 gap-x-6 gap-y-9 min-[400px]:grid-cols-2 md:grid-cols-3 lg:grid-cols-4">
          {products.map((product) => (
            <li key={product.id}>
              <div className="relative aspect-square overflow-hidden rounded-sm border border-neutral-200 bg-white">
                <Image
                  src={product.image_url}
                  alt={product.title}
                  fill
                  unoptimized
                  sizes="(max-width: 399px) 100vw, (max-width: 767px) 50vw, 25vw"
                  className="object-contain p-3"
                />
              </div>
              <h2 className="mt-3 text-base leading-snug font-medium wrap-anywhere">
                {product.title}
              </h2>
            </li>
          ))}
        </ul>
      )}
      {products.length === 100 && (
        <a
          className="mt-10 inline-block py-3 underline underline-offset-4 focus-visible:outline-2"
          href={`/?before=${products[products.length - 1].id}`}
        >
          Следующие товары
        </a>
      )}
    </main>
  );
}
