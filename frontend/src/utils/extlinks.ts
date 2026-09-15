// 外部详情页链接构造（与后端返回的 douban_id / imdb_id 对齐）。
// 豆瓣 subject id 与 IMDb title id 不等同于内部主键，后端已单独带回。

export function doubanUrl(doubanId?: string | null): string | null {
  if (!doubanId) return null
  const id = String(doubanId).trim()
  if (!id) return null
  return `https://movie.douban.com/subject/${id}`
}

export function imdbUrl(imdbId?: string | null): string | null {
  if (!imdbId) return null
  const id = String(imdbId).trim()
  if (!id) return null
  return `https://www.imdb.com/title/${id}`
}
