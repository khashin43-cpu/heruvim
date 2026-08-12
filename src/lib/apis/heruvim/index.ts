import { WEBUI_API_BASE_URL } from '$lib/constants';

const request = async (token: string, path: string, init: RequestInit = {}) => {
	let response: Response;
	try {
		response = await fetch(`${WEBUI_API_BASE_URL}/heruvim${path}`, {
			...init,
			headers: {
				Accept: 'application/json',
				authorization: `Bearer ${token}`,
				...(init.headers ?? {})
			}
		});
	} catch {
		throw new Error('Не удалось связаться с ХЕРУВИМом. Проверьте, что сервер запущен');
	}
	if (!response.ok) {
		const error = await response.json().catch(() => ({}));
		throw new Error(error?.detail ?? 'Не удалось получить состояние RAGFlow');
	}
	return response.json();
};

export const getIngestionStatus = (token: string) => request(token, '/ingestion');

export const retryIngestion = (token: string, fileId: string) =>
	request(token, `/ingestion/${encodeURIComponent(fileId)}/retry`, { method: 'POST' });

export const getKnowledgeDocuments = (token: string) => request(token, '/ragflow/documents');

export const deleteKnowledgeDocument = (token: string, datasetId: string, documentId: string) =>
	request(
		token,
		`/ragflow/datasets/${encodeURIComponent(datasetId)}/documents/${encodeURIComponent(documentId)}`,
		{ method: 'DELETE' }
	);

export const getKnowledgeDocumentBlob = async (
	token: string,
	documentId: string,
	download = false
) => {
	const response = await fetch(
		`${WEBUI_API_BASE_URL}/heruvim/ragflow/documents/${encodeURIComponent(documentId)}/preview${download ? '?download=1' : ''}`,
		{
			headers: { authorization: `Bearer ${token}` }
		}
	);
	if (!response.ok) {
		const error = await response.json().catch(() => ({}));
		throw new Error(error?.detail ?? 'Не удалось открыть документ');
	}
	return response.blob();
};
