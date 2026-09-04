import client from './client'

export const createServiceGit = (data: Record<string, any>) => client.post('/services', data)

// Lightweight convert/preview (design §Implementation notes L284-286):
// volume-override keys come from the converter's in-call src→key mapping,
// NOT from parsing .j2 templates in the frontend.
// The query string is pre-serialized: FastAPI list params need REPEATED
// ?compose_files= keys (axios' default bracket notation compose_files[] is
// ignored by FastAPI and would leave the param empty).
export const getComposePreview = (
  serviceName: string,
  composeFiles: string[],
  recipePath: string = '',
) => {
  const qs = [
    ...composeFiles.map(f => `compose_files=${encodeURIComponent(f)}`),
    ...(recipePath ? [`recipe_path=${encodeURIComponent(recipePath)}`] : []),
  ].join('&')
  return client.get(`/services/${serviceName}/compose-preview?${qs}`)
}

// Profile candidates derived from an IN-PANEL compose selection (GAP-2):
// the panels recompute the profiles section when the compose selection
// changes, so it reflects the merged compose (design §Selection & UI L59-62)
// instead of only the stored file set.
export const deriveProfiles = (
  serviceName: string,
  composeFiles: string[],
  recipePath: string = '',
) => client.post(`/services/${serviceName}/file-sets/derive`, {
  recipe_path: recipePath,
  compose: composeFiles,
})
