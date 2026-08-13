import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import { AuthProvider } from './hooks/useAuth';
import { OverlayProvider } from './components/ui/OverlayContext';
import './styles/global.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
    <AuthProvider>
      <OverlayProvider>
        <App />
      </OverlayProvider>
    </AuthProvider>
  </BrowserRouter>
);
