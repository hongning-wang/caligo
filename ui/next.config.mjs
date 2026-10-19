export default {
  poweredByHeader: false,
  ...(process.env.NODE_ENV === "development"
    ? {
        async rewrites() {
          return [
            {
              source: "/api/:path*",
              destination: `${process.env.CALIGO_API_URL || "http://127.0.0.1:8765"}/api/:path*`,
            },
          ];
        },
      }
    : { output: "export" }),
};
