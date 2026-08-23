import axios from 'axios'

const client = axios.create({
  baseURL: '/api',
  timeout: 30000,
  withCredentials: true,
  headers: {
    'Content-Type': 'application/json',
  },
})

// v4 §11.2 (N5): auth is carried by the provision_token cookie (HttpOnly),
// auto-sent by the browser. No Bearer header is attached and no access_token /
// refresh_token are stored in localStorage, so no refresh interceptor exists.

export default client
